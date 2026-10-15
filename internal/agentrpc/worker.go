// Package agentrpc drives the Python brain. Each worker is one Python process serving turns one
// at a time, speaking the line protocol `proto/rpc.json` and `crates/core/src/ipc.rs` already
// define: one JSON message per line in, one reply per line out,
// `{"id", "method", "params"}` -> `{"id", "result"}` or `{"id", "error"}`.
package agentrpc

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"sync"
	"time"
)

// Config is how to start one worker process.
type Config struct {
	Command []string // interpreter and its flags, e.g. {"py", "-3"}
	Script  string   // absolute path to agent/src/cli.py
	Dir     string   // the worker's working directory: its state.db lands here
	Env     []string // extra environment entries, "K=V"
	Log     io.Writer
	Timeout time.Duration // for one turn; default 5m
}

// Worker is one Python process. One call at a time, by construction: the brain's live context is
// instance state, so two overlapping turns in one process would interleave into one transcript.
type Worker struct {
	cfg    Config
	mu     sync.Mutex
	cmd    *exec.Cmd
	stdin  io.WriteCloser
	stdout *bufio.Reader
	nextID uint64
	dead   error
	closed bool
}

type reply struct {
	ID     uint64          `json:"id"`
	Result json.RawMessage `json:"result"`
	Error  string          `json:"error"`
}

// Start spawns the worker process.
func Start(cfg Config) (*Worker, error) {
	if len(cfg.Command) == 0 {
		return nil, errors.New("agentrpc: no interpreter configured")
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = 5 * time.Minute
	}
	if cfg.Log == nil {
		cfg.Log = os.Stderr
	}
	w := &Worker{cfg: cfg}
	if err := w.spawn(); err != nil {
		return nil, err
	}
	return w, nil
}

func (w *Worker) spawn() error {
	args := append(append([]string{}, w.cfg.Command[1:]...), w.cfg.Script, "--serve-rpc")
	cmd := exec.Command(w.cfg.Command[0], args...)
	cmd.Dir = w.cfg.Dir
	if len(w.cfg.Env) > 0 {
		cmd.Env = append(os.Environ(), w.cfg.Env...)
	}
	// The worker keeps its own log on stderr, so stdout carries protocol lines only.
	cmd.Stderr = w.cfg.Log
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return fmt.Errorf("agentrpc: stdin pipe: %w", err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return fmt.Errorf("agentrpc: stdout pipe: %w", err)
	}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("agentrpc: start %s: %w", w.cfg.Script, err)
	}
	w.cmd, w.stdin, w.stdout, w.dead, w.closed = cmd, stdin, bufio.NewReader(stdout), nil, false
	return nil
}

// Call sends one request and waits for its reply, honouring ctx.
func (w *Worker) Call(ctx context.Context, method string, params any, out any) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return errors.New("agentrpc: worker is closed")
	}
	if w.dead != nil {
		return w.dead
	}
	w.nextID++
	id := w.nextID
	payload, err := json.Marshal(map[string]any{"id": id, "method": method, "params": params})
	if err != nil {
		return fmt.Errorf("agentrpc: encode %s: %w", method, err)
	}
	if _, err := w.stdin.Write(append(payload, '\n')); err != nil {
		return w.fail(fmt.Errorf("agentrpc: write to worker: %w", err))
	}

	for {
		line, err := w.readLine(ctx)
		if err != nil {
			return w.fail(err)
		}
		var r reply
		if err := json.Unmarshal(line, &r); err != nil {
			return w.fail(fmt.Errorf("agentrpc: worker sent a line that is not a reply: %w", err))
		}
		if r.ID != id {
			// A reply to a call this worker already lost. Skipping it is what keeps it from
			// being read as the answer to this one.
			continue
		}
		if r.Error != "" {
			return errors.New(r.Error)
		}
		if out != nil && len(r.Result) > 0 {
			if err := json.Unmarshal(r.Result, out); err != nil {
				return fmt.Errorf("agentrpc: decode %s result: %w", method, err)
			}
		}
		return nil
	}
}

// readLine reads one protocol line, giving up when ctx does. Giving up kills the worker: it is
// still busy with the turn we stopped waiting for, and its next reply would otherwise look like
// the answer to whatever we ask next.
func (w *Worker) readLine(ctx context.Context) ([]byte, error) {
	type result struct {
		line []byte
		err  error
	}
	ch := make(chan result, 1)
	go func() {
		line, err := w.stdout.ReadBytes('\n')
		ch <- result{line, err}
	}()
	select {
	case r := <-ch:
		if r.err != nil {
			if errors.Is(r.err, io.EOF) {
				return nil, errors.New("agentrpc: worker exited")
			}
			return nil, fmt.Errorf("agentrpc: read from worker: %w", r.err)
		}
		return r.line, nil
	case <-ctx.Done():
		return nil, fmt.Errorf("agentrpc: %w", ctx.Err())
	}
}

// fail records why the worker is unusable and stops it. Callers hold w.mu.
func (w *Worker) fail(err error) error {
	w.dead = err
	w.kill()
	return err
}

func (w *Worker) kill() {
	if w.stdin != nil {
		_ = w.stdin.Close()
	}
	if w.cmd != nil && w.cmd.Process != nil {
		_ = w.cmd.Process.Kill()
		_, _ = w.cmd.Process.Wait()
	}
}

// Dead reports why the worker is unusable, or nil while it is alive.
func (w *Worker) Dead() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.dead
}

// Close stops the worker: a closed stdin ends its read loop.
func (w *Worker) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return nil
	}
	w.closed = true
	w.kill()
	return nil
}

// Ping checks that the worker answers, and returns what it reports about itself.
func (w *Worker) Ping(ctx context.Context) (string, error) {
	var out struct {
		Status string `json:"status"`
		Model  string `json:"model"`
	}
	if err := w.Call(ctx, "ping", map[string]any{}, &out); err != nil {
		return "", err
	}
	if out.Status != "pong" {
		return "", fmt.Errorf("agentrpc: worker answered %q to ping", out.Status)
	}
	return out.Model, nil
}
