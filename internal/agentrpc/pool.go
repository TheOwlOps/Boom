package agentrpc

import (
	"context"
	"errors"
	"sync"
	"time"
)

// TurnRequest is one message for the brain. The session is the conversation: the edge keys it per
// group, so a group's context stays its own.
type TurnRequest struct {
	SessionID  string `json:"session_id"`
	Channel    string `json:"channel,omitempty"`
	SenderName string `json:"sender_name,omitempty"`
	Text       string `json:"text"`
}

// TurnResult is what the brain answered.
type TurnResult struct {
	Action  string  `json:"action"`
	Success bool    `json:"success"`
	Steps   int     `json:"steps"`
	Error   *string `json:"error"`
}

// Pool runs `size` workers and hands each turn to one of them. A session prefers the worker that
// served it last (its in-memory context is the fuller one); when that worker is busy the turn goes
// to a free one, which rebuilds the context from the transcript.
type Pool struct {
	cfg    Config
	mu     sync.Mutex
	cond   *sync.Cond
	workers []*Worker
	free    []bool
	sticky  map[string]int
	closed  bool
}

// New starts size workers. One dead on arrival is not fatal: the rest can still serve turns.
func New(cfg Config, size int) (*Pool, error) {
	if size < 1 {
		size = 1
	}
	p := &Pool{cfg: cfg, free: make([]bool, size), sticky: map[string]int{}}
	p.cond = sync.NewCond(&p.mu)
	var lastErr error
	for i := 0; i < size; i++ {
		w, err := Start(cfg)
		if err != nil {
			lastErr = err
			continue
		}
		p.workers = append(p.workers, w)
		p.free[len(p.workers)-1] = true
	}
	if len(p.workers) == 0 {
		return nil, lastErr
	}
	return p, nil
}

// Turn runs one turn on a free worker. The caller gets the error of a turn that could not run:
// a turn is never retried here, because a failed one may already have changed the workspace.
func (p *Pool) Turn(ctx context.Context, req TurnRequest) (TurnResult, error) {
	idx, worker, err := p.acquire(req.SessionID)
	if err != nil {
		return TurnResult{}, err
	}
	timeout := p.cfg.Timeout
	if timeout <= 0 {
		timeout = defaultTimeout
	}
	callCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	var res TurnResult
	err = worker.Call(callCtx, "turn", req, &res)
	p.release(idx, req.SessionID, err)
	return res, err
}

// Ping asks the first live worker whether it answers. It reports the model it is configured with.
func (p *Pool) Ping(ctx context.Context) (string, error) {
	p.mu.Lock()
	workers := append([]*Worker{}, p.workers...)
	p.mu.Unlock()
	var lastErr error
	for _, w := range workers {
		if w.Dead() != nil {
			continue
		}
		model, err := w.Ping(ctx)
		if err == nil {
			return model, nil
		}
		lastErr = err
	}
	if lastErr == nil {
		lastErr = errors.New("agentrpc: no worker is alive")
	}
	return "", lastErr
}

// Size is how many workers the pool was asked for, Alive how many are usable right now.
func (p *Pool) Size() int { return len(p.workers) }

func (p *Pool) Alive() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	alive := 0
	for _, w := range p.workers {
		if w.Dead() == nil {
			alive++
		}
	}
	return alive
}

// Busy is how many workers are running a turn.
func (p *Pool) Busy() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	busy := 0
	for _, free := range p.free {
		if !free {
			busy++
		}
	}
	return busy
}

func (p *Pool) acquire(session string) (int, *Worker, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for {
		if p.closed {
			return -1, nil, errors.New("agentrpc: pool is closed")
		}
		if i, ok := p.pick(session); ok {
			p.free[i] = false
			return i, p.workers[i], nil
		}
		if p.aliveLocked() == 0 {
			return -1, nil, errors.New("agentrpc: no worker is alive")
		}
		p.cond.Wait()
	}
}

// pick chooses a free worker: the session's own first, then any. Callers hold p.mu.
func (p *Pool) pick(session string) (int, bool) {
	if session != "" {
		if i, ok := p.sticky[session]; ok && i < len(p.free) && p.free[i] {
			return i, true
		}
	}
	for i, free := range p.free {
		if free && p.workers[i].Dead() == nil {
			return i, true
		}
	}
	return -1, false
}

func (p *Pool) aliveLocked() int {
	alive := 0
	for _, w := range p.workers {
		if w.Dead() == nil {
			alive++
		}
	}
	return alive
}

// release returns a worker to the pool, replacing it if it died. A dead worker is never reused:
// its brain state is gone with the process, and a fresh process starts clean.
func (p *Pool) release(idx int, session string, callErr error) {
	worker := p.workers[idx]
	var replacementErr error
	if worker.Dead() != nil {
		worker.Close()
		replacement, err := Start(p.cfg)
		if err != nil {
			replacementErr = err
		} else {
			p.workers[idx] = replacement
		}
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if callErr == nil && session != "" {
		p.sticky[session] = idx
	}
	if replacementErr != nil {
		// The slot stays unusable rather than freeing a dead process for the next turn.
		p.free[idx] = false
	} else {
		p.free[idx] = true
	}
	p.cond.Broadcast()
}

// Close stops every worker.
func (p *Pool) Close() {
	p.mu.Lock()
	p.closed = true
	workers := append([]*Worker{}, p.workers...)
	p.mu.Unlock()
	for _, w := range workers {
		_ = w.Close()
	}
	p.mu.Lock()
	p.cond.Broadcast()
	p.mu.Unlock()
}

const defaultTimeout = 5 * time.Minute
