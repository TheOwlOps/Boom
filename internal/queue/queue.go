package queue

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strconv"
	"sync"
	"time"
)

const (
	defaultConcurrency        = 2
	defaultMaxQueued          = 32 // per channel
	defaultRetries            = 1  // extra attempts after a turn raises
	defaultRetryDelay         = 2.0
	totalQueueFactor          = 4 // total waiting cap = max_queued * this
	notRunning         string = "not running"
)

// Outcome is what a caller gets: the job's fate, and the reply when there is one.
type Outcome struct {
	Status   string `json:"status"`
	JobID    string `json:"job_id"`
	Position int    `json:"position"`
	Channel  string `json:"channel,omitempty"`
	Reply    string `json:"reply,omitempty"`
	Error    string `json:"error,omitempty"`
}

// Config is the dispatcher's shape. Zero values mean "take the default or the environment".
type Config struct {
	RootDir string // memories/ lives here; empty means the queue is not persisted

	Concurrency        int
	MaxQueued          int
	ChannelConcurrency int
	ChannelLimits      map[string]int

	// Retries and RetryDelay distinguish "unset" from "zero", which is why they are pointers.
	Retries    *int
	RetryDelay *float64

	// FailureText is what a channel is told when a turn fails and there is no caller left to
	// report it to. Without it a queued turn would fail and the user would hear nothing at all.
	FailureText string

	// Runner runs one turn. It must honour ctx: a stopping dispatcher cancels what is running.
	Runner func(ctx context.Context, job *Job) (string, error)
	// Deliver sends a reply for a job whose caller is gone.
	Deliver func(job *Job, reply string) error
	Log     func(format string, args ...any)
}

// channel is one channel's waiting line and its share of the pool.
type channel struct {
	name    string
	queue   []*Job
	running int
	limit   int
}

// Dispatcher is the bounded pool: the only thing that runs turns, so the cap is the cap.
type Dispatcher struct {
	cfg  Config
	path string

	mu       sync.Mutex
	cond     *sync.Cond
	channels map[string]*channel
	order    []string // rotation order, in the order channels first appeared
	cursor   int
	jobs     map[string]*Job
	all      []*Job // every job this process has seen, in submission order (for saving)

	waiters         map[string]chan Outcome
	callbacks       map[string]func(string) error
	cancelRequested map[string]bool
	runningJobs     map[string]*Job
	jobCancel       map[string]context.CancelFunc

	workers    sync.WaitGroup
	turns      sync.WaitGroup
	deliveries sync.WaitGroup
	backoffs   sync.WaitGroup

	ctx     context.Context
	cancel  context.CancelFunc
	stopped bool

	delivered, failed, rejected, cancelled, retried, queued int
	delivering, retrying                                    int
}

// New builds a dispatcher. Call Start to spawn the pool.
func New(cfg Config) *Dispatcher {
	if cfg.Concurrency <= 0 {
		cfg.Concurrency = envInt("BOOM_GATEWAY_CONCURRENCY", defaultConcurrency)
	}
	if cfg.MaxQueued <= 0 {
		cfg.MaxQueued = envInt("BOOM_GATEWAY_QUEUE", defaultMaxQueued)
	}
	// A channel may use the whole pool unless it is capped; the cap is what makes one busy chat
	// unable to hold every slot.
	if cfg.ChannelConcurrency <= 0 {
		cfg.ChannelConcurrency = envInt("BOOM_GATEWAY_CHANNEL_CONCURRENCY", cfg.Concurrency)
	}
	if cfg.ChannelLimits == nil {
		cfg.ChannelLimits = map[string]int{}
	}
	if cfg.Log == nil {
		cfg.Log = func(string, ...any) {}
	}
	ctx, cancel := context.WithCancel(context.Background())
	d := &Dispatcher{
		cfg:             cfg,
		path:            jobsPath(cfg.RootDir),
		channels:        map[string]*channel{},
		jobs:            map[string]*Job{},
		waiters:         map[string]chan Outcome{},
		callbacks:       map[string]func(string) error{},
		cancelRequested: map[string]bool{},
		runningJobs:     map[string]*Job{},
		jobCancel:       map[string]context.CancelFunc{},
		ctx:             ctx,
		cancel:          cancel,
	}
	d.cond = sync.NewCond(&d.mu)
	return d
}

// Start spawns the workers and re-queues what an earlier run left unfinished.
func (d *Dispatcher) Start() {
	d.mu.Lock()
	recovered := loadJobs(d.path)
	for _, job := range recovered {
		d.enqueueLocked(job)
		d.cfg.Log("[queue] re-queued %s from a previous run (%s %s)",
			job.JobID, job.Channel, job.SenderID)
	}
	d.mu.Unlock()
	d.workers.Add(d.cfg.Concurrency)
	for i := 0; i < d.cfg.Concurrency; i++ {
		go d.worker()
	}
	if len(recovered) > 0 {
		d.wake()
	}
}

// Stop cancels what is running and saves the queue. A turn that was running stays unfinished on
// disk and is re-queued by the next Start: its reply was never sent.
func (d *Dispatcher) Stop() {
	d.mu.Lock()
	if d.stopped {
		d.mu.Unlock()
		return
	}
	d.stopped = true
	d.cancel()
	for id, waiter := range d.waiters {
		// Nobody is going to run these now; a caller left waiting would wait forever.
		if job := d.jobs[id]; job != nil {
			waiter <- Outcome{Status: job.Status, JobID: id}
		}
		delete(d.waiters, id)
	}
	d.cond.Broadcast()
	d.mu.Unlock()

	d.workers.Wait()
	d.turns.Wait()
	d.backoffs.Wait()
	d.deliveries.Wait() // a reply that was already on its way should still land
	d.save()
}

// ---------------------------------------------------------------- channels

func (d *Dispatcher) limitFor(name string) int {
	limit, ok := d.cfg.ChannelLimits[name]
	if !ok {
		limit = d.cfg.ChannelConcurrency
	}
	if limit > d.cfg.Concurrency {
		limit = d.cfg.Concurrency
	}
	if limit < 1 {
		limit = 1
	}
	return limit
}

// channelLocked returns a channel's state, creating it on first use. Callers hold d.mu.
func (d *Dispatcher) channelLocked(name string) *channel {
	state, ok := d.channels[name]
	if !ok {
		state = &channel{name: name, limit: d.limitFor(name)}
		d.channels[name] = state
		d.order = append(d.order, name)
	}
	return state
}

// enqueueLocked adds to the channel's line: priority orders it, equal priority stays first-in
// first-out. Callers hold d.mu.
func (d *Dispatcher) enqueueLocked(job *Job) {
	state := d.channelLocked(job.Channel)
	d.jobs[job.JobID] = job
	job.Status = statusQueued
	if job.Priority > 0 {
		at := len(state.queue)
		for i, waiting := range state.queue {
			if waiting.Priority < job.Priority {
				at = i
				break
			}
		}
		state.queue = append(state.queue, nil)
		copy(state.queue[at+1:], state.queue[at:])
		state.queue[at] = job
	} else {
		state.queue = append(state.queue, job)
	}
	if !containsJob(d.all, job) {
		d.all = append(d.all, job)
	}
	d.queued++
}

func containsJob(jobs []*Job, job *Job) bool {
	for _, j := range jobs {
		if j.JobID == job.JobID {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------- submit

// Submit queues a turn. `wait` holds the caller until it has run; otherwise the caller is acked.
// Nobody runs in-band here: the pool is the only thing that runs turns.
func (d *Dispatcher) Submit(job *Job, deliver func(string) error, wait bool) Outcome {
	d.mu.Lock()
	if d.stopped {
		d.mu.Unlock()
		return Outcome{Status: "rejected", JobID: job.JobID, Error: "queue is stopped"}
	}
	state := d.channelLocked(job.Channel)
	if len(state.queue) >= d.cfg.MaxQueued || d.queued >= d.cfg.MaxQueued*totalQueueFactor {
		d.rejected++
		detail := fmt.Sprintf("queue is full (%d/%d on %s)", len(state.queue), d.cfg.MaxQueued, job.Channel)
		d.mu.Unlock()
		return Outcome{Status: "rejected", JobID: job.JobID, Error: detail}
	}
	d.enqueueLocked(job)
	position := len(state.queue) - 1
	if deliver != nil {
		d.callbacks[job.JobID] = deliver
	}
	var waiter chan Outcome
	if wait {
		waiter = make(chan Outcome, 1)
		d.waiters[job.JobID] = waiter
	}
	d.saveLocked()
	d.mu.Unlock()
	d.wake()

	if !wait {
		return Outcome{Status: "queued", JobID: job.JobID, Position: position}
	}
	return <-waiter
}

// Cancel stops a job: a queued one is dropped, a running one is cancelled at its next await.
func (d *Dispatcher) Cancel(jobID string) Outcome {
	d.mu.Lock()
	defer d.mu.Unlock()
	job, ok := d.jobs[jobID]
	if !ok {
		return Outcome{Status: "unknown", JobID: jobID}
	}
	if cancel, running := d.jobCancel[jobID]; running {
		d.cancelRequested[jobID] = true
		cancel()
		return Outcome{Status: "cancelling", JobID: jobID, Channel: job.Channel}
	}
	if d.dropQueuedLocked(job, "cancelled before it ran") {
		d.saveLocked()
		return Outcome{Status: "cancelled", JobID: jobID, Channel: job.Channel}
	}
	return Outcome{Status: notRunning, JobID: jobID, Channel: job.Channel}
}

// CancelChannel stops everything on one channel: waiting jobs are dropped, running ones cancelled.
func (d *Dispatcher) CancelChannel(name string) (queuedStopped, runningStopped int) {
	d.mu.Lock()
	defer d.mu.Unlock()
	state, ok := d.channels[name]
	if !ok {
		return 0, 0
	}
	for _, job := range append([]*Job{}, state.queue...) {
		if d.dropQueuedLocked(job, "cancelled with the channel") {
			queuedStopped++
		}
	}
	for jobID, job := range d.runningJobs {
		if job.Channel != name {
			continue
		}
		d.cancelRequested[jobID] = true
		d.jobCancel[jobID]()
		runningStopped++
	}
	d.saveLocked()
	return queuedStopped, runningStopped
}

// dropQueuedLocked takes a job out of its channel's line. False if it was not waiting there.
func (d *Dispatcher) dropQueuedLocked(job *Job, reason string) bool {
	state, ok := d.channels[job.Channel]
	if !ok {
		return false
	}
	found := false
	kept := state.queue[:0]
	for _, waiting := range state.queue {
		if waiting.JobID == job.JobID {
			found = true
			continue
		}
		kept = append(kept, waiting)
	}
	if !found {
		return false
	}
	state.queue = kept
	d.queued--
	job.Status = statusCancelled
	d.cancelled++
	d.settleLocked(job, nil, reason)
	return true
}

// ---------------------------------------------------------------- the pool

func (d *Dispatcher) wake() {
	d.mu.Lock()
	d.cond.Broadcast()
	d.mu.Unlock()
}

func (d *Dispatcher) worker() {
	defer d.workers.Done()
	for {
		d.mu.Lock()
		for {
			if d.stopped {
				d.mu.Unlock()
				return
			}
			job, state, ok := d.pickLocked()
			if !ok {
				d.cond.Wait()
				continue
			}
			d.mu.Unlock()
			d.turns.Add(1)
			d.execute(job)
			d.turns.Done()

			d.mu.Lock()
			state.running--
			if len(state.queue) == 0 && state.running == 0 {
				d.dropChannelLocked(state.name)
			}
			d.saveLocked()
			d.mu.Unlock()
			d.wake()
			break
		}
	}
}

func (d *Dispatcher) dropChannelLocked(name string) {
	delete(d.channels, name)
	for i, existing := range d.order {
		if existing == name {
			d.order = append(d.order[:i], d.order[i+1:]...)
			if d.cursor > i {
				d.cursor--
			}
			if len(d.order) > 0 {
				d.cursor %= len(d.order)
			} else {
				d.cursor = 0
			}
			break
		}
	}
}

// pickLocked takes the next job: the most urgent head if one is waiting, else the next channel in
// rotation. Round-robin is the fairness between channels; priority is the explicit exception to
// it, so a job has to be marked to jump the rotation. Callers hold d.mu.
func (d *Dispatcher) pickLocked() (*Job, *channel, bool) {
	urgent := -1
	for _, name := range d.order {
		state := d.channels[name]
		if len(state.queue) == 0 || state.running >= state.limit {
			continue
		}
		if state.queue[0].Priority > urgent {
			urgent = state.queue[0].Priority
		}
	}
	if urgent < 0 {
		return nil, nil, false
	}
	if urgent > 0 {
		for _, name := range d.order {
			state := d.channels[name]
			if len(state.queue) > 0 && state.running < state.limit &&
				state.queue[0].Priority == urgent {
				return d.takeLocked(state)
			}
		}
	}
	for i := 0; i < len(d.order); i++ {
		name := d.order[(d.cursor+i)%len(d.order)]
		state := d.channels[name]
		if len(state.queue) > 0 && state.running < state.limit {
			d.cursor = (d.cursor + i + 1) % len(d.order)
			return d.takeLocked(state)
		}
	}
	return nil, nil, false
}

func (d *Dispatcher) takeLocked(state *channel) (*Job, *channel, bool) {
	job := state.queue[0]
	state.queue = state.queue[1:]
	d.queued--
	state.running++
	return job, state, true
}

// execute runs one job and settles it: delivered, retried, failed or cancelled.
func (d *Dispatcher) execute(job *Job) {
	d.mu.Lock()
	job.Status = statusRunning
	job.Attempts++
	d.runningJobs[job.JobID] = job
	turnCtx, cancel := context.WithCancel(d.ctx)
	d.jobCancel[job.JobID] = cancel
	d.mu.Unlock()

	reply, err := d.cfg.Runner(turnCtx, job)
	cancel()

	d.mu.Lock()
	delete(d.jobCancel, job.JobID)
	delete(d.runningJobs, job.JobID)
	wasCancelled := d.cancelRequested[job.JobID]
	stopping := d.stopped
	if wasCancelled {
		delete(d.cancelRequested, job.JobID)
	}
	d.mu.Unlock()

	switch {
	case wasCancelled:
		job.Status = statusCancelled
		d.count(&d.cancelled)
		d.cfg.Log("[queue] %s cancelled while running", job.JobID)
		d.settle(job, nil, "cancelled while running")
	case err != nil && errors.Is(err, context.Canceled) && stopping:
		// The pool is stopping: the job stays unfinished on disk and is re-queued.
	case err != nil:
		d.cfg.Log("[queue] %s attempt %d failed: %v", job.JobID, job.Attempts, err)
		if job.Attempts <= d.retries() {
			d.count(&d.retried)
			// It is waiting, not running: the worker is free for another job meanwhile.
			job.Status = statusQueued
			d.backoff(job)
			return
		}
		job.Status = statusFailed
		d.count(&d.failed)
		d.settle(job, nil, err.Error())
	default:
		job.Status = statusDone
		d.count(&d.delivered)
		d.settle(job, &reply, "")
	}
}

// backoff waits out the doubling delay, then puts the job back at the front of its line: it has
// already waited its turn once. The job stays unfinished on disk meanwhile.
func (d *Dispatcher) backoff(job *Job) {
	delay := d.retryDelay() * float64(int(1)<<uint(job.Attempts-1))
	d.mu.Lock()
	d.retrying++
	d.mu.Unlock()
	d.backoffs.Add(1)
	go func() {
		defer d.backoffs.Done()
		defer func() {
			d.mu.Lock()
			d.retrying--
			d.mu.Unlock()
		}()
		timer := time.NewTimer(time.Duration(delay * float64(time.Second)))
		defer timer.Stop()
		select {
		case <-timer.C:
		case <-d.ctx.Done():
			return // stopping: the job stays queued on disk and the next run picks it up
		}
		d.mu.Lock()
		if d.stopped {
			d.mu.Unlock()
			return
		}
		state := d.channelLocked(job.Channel)
		job.Status = statusQueued
		state.queue = append([]*Job{job}, state.queue...)
		d.queued++
		d.mu.Unlock()
		d.wake()
	}()
}

// settle hands the outcome to whoever is waiting for it, then delivers it if nobody is.
func (d *Dispatcher) settle(job *Job, reply *string, errText string) {
	d.mu.Lock()
	d.settleLocked(job, reply, errText)
	d.mu.Unlock()
}

func (d *Dispatcher) settleLocked(job *Job, reply *string, errText string) {
	out := Outcome{Status: job.Status, JobID: job.JobID, Error: errText}
	if reply != nil {
		out.Reply = *reply
	}
	waiter := d.waiters[job.JobID]
	delete(d.waiters, job.JobID)
	deliver := d.callbacks[job.JobID]
	delete(d.callbacks, job.JobID)
	if waiter != nil {
		waiter <- out
		return
	}
	text := ""
	if reply != nil {
		text = *reply
	} else {
		if job.Status != statusFailed || d.cfg.FailureText == "" {
			return // nothing to deliver, and inventing a reply is not an option
		}
		text = d.cfg.FailureText
	}
	d.delivering++
	d.deliveries.Add(1)
	go func() {
		defer d.deliveries.Done()
		defer func() {
			d.mu.Lock()
			d.delivering--
			d.mu.Unlock()
		}()
		d.deliver(job, text, deliver)
	}()
}

func (d *Dispatcher) deliver(job *Job, reply string, deliver func(string) error) {
	var err error
	switch {
	case deliver != nil:
		err = deliver(reply)
	case d.cfg.Deliver != nil:
		// No caller left: the job came back from disk, or the channel is one the daemon reaches
		// on its own.
		err = d.cfg.Deliver(job, reply)
	default:
		d.cfg.Log("[queue] %s finished with no way to deliver the reply", job.JobID)
		return
	}
	if err != nil {
		d.cfg.Log("[queue] delivery of %s failed: %v", job.JobID, err)
	}
}

func (d *Dispatcher) count(counter *int) {
	d.mu.Lock()
	*counter++
	d.mu.Unlock()
}

func (d *Dispatcher) retries() int {
	if d.cfg.Retries != nil {
		return *d.cfg.Retries
	}
	return envInt("BOOM_GATEWAY_RETRIES", defaultRetries)
}

func (d *Dispatcher) retryDelay() float64 {
	if d.cfg.RetryDelay != nil {
		return *d.cfg.RetryDelay
	}
	return envFloat("BOOM_GATEWAY_RETRY_DELAY", defaultRetryDelay)
}

func (d *Dispatcher) save() {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.saveLocked()
}

func (d *Dispatcher) saveLocked() {
	if err := saveJobs(d.path, d.all); err != nil {
		d.cfg.Log("[queue] could not save the queue: %v", err)
	}
}

// ---------------------------------------------------------------- status

// RunningJob is one turn in flight.
type RunningJob struct {
	JobID   string `json:"job_id"`
	Channel string `json:"channel"`
}

// ChannelStatus is one channel's share of the pool.
type ChannelStatus struct {
	Running int `json:"running"`
	Queued  int `json:"queued"`
	Limit   int `json:"limit"`
}

// Status is what the operator needs to see: what is running, what is waiting, what went wrong.
type Status struct {
	Concurrency        int                      `json:"concurrency"`
	ChannelConcurrency int                      `json:"channel_concurrency"`
	Running            int                      `json:"running"`
	Queued             int                      `json:"queued"`
	MaxQueued          int                      `json:"max_queued"`
	Delivering         int                      `json:"delivering"`
	Retrying           int                      `json:"retrying"`
	Delivered          int                      `json:"delivered"`
	Failed             int                      `json:"failed"`
	Rejected           int                      `json:"rejected"`
	Cancelled          int                      `json:"cancelled"`
	Retries            int                      `json:"retries"`
	Retried            int                      `json:"retried"`
	RunningJobs        []RunningJob             `json:"running_jobs"`
	Channels           map[string]ChannelStatus `json:"channels"`
}

func (d *Dispatcher) Status() Status {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := Status{
		Concurrency:        d.cfg.Concurrency,
		ChannelConcurrency: d.cfg.ChannelConcurrency,
		Queued:             d.queued,
		MaxQueued:          d.cfg.MaxQueued,
		Delivering:         d.delivering,
		Retrying:           d.retrying,
		Delivered:          d.delivered,
		Failed:             d.failed,
		Rejected:           d.rejected,
		Cancelled:          d.cancelled,
		Retries:            d.retries(),
		Retried:            d.retried,
		RunningJobs:        make([]RunningJob, 0, len(d.runningJobs)),
		Channels:           map[string]ChannelStatus{},
	}
	for _, state := range d.channels {
		out.Running += state.running
		out.Channels[state.name] = ChannelStatus{
			Running: state.running, Queued: len(state.queue), Limit: state.limit,
		}
	}
	for _, job := range d.runningJobs {
		out.RunningJobs = append(out.RunningJobs, RunningJob{JobID: job.JobID, Channel: job.Channel})
	}
	return out
}

// Job reports a job this process has seen, for the admin commands.
func (d *Dispatcher) Job(jobID string) (*Job, bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	job, ok := d.jobs[jobID]
	return job, ok
}

func envInt(name string, fallback int) int {
	value, err := strconv.Atoi(os.Getenv(name))
	if err != nil {
		return fallback
	}
	return value
}

func envFloat(name string, fallback float64) float64 {
	value, err := strconv.ParseFloat(os.Getenv(name), 64)
	if err != nil {
		return fallback
	}
	return value
}
