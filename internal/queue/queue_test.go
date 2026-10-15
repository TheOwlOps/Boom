package queue

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// This is the Go port of the behaviour `evals/test_gateway_dispatcher.py` pins down: the pool cap
// and the per-channel cap hold, the round-robin serves a quiet channel while a noisy one is
// flooded, a full queue is refused instead of growing, a running or waiting turn can be cancelled,
// a failing turn is reported as failed, and a job accepted before a restart is re-run, not lost.
// There is no model to call offline, so the runner is a stand-in that does real work.
//
// Where a test asserts the order jobs run in, jobs are queued one at a time in the main goroutine:
// submitting them from several goroutines at once would leave the line in whatever order they
// happened to win the lock, and then the assertion would be about the scheduler, not the pool.

type testRunner struct {
	mu       sync.Mutex
	delay    time.Duration
	failOn   map[string]bool
	failOnce map[string]bool
	gated    map[string]bool
	gate     chan struct{}
	gateOnce sync.Once

	active    int
	peak      int
	activeBy  map[string]int
	peakBy    map[string]int
	order     []string
	seen      map[string]int
	cancelled int
}

func newRunner(delay time.Duration, failOn, failOnce, gated []string) *testRunner {
	r := &testRunner{
		delay: delay, gate: make(chan struct{}),
		failOn: map[string]bool{}, failOnce: map[string]bool{}, gated: map[string]bool{},
		activeBy: map[string]int{}, peakBy: map[string]int{}, seen: map[string]int{},
	}
	for _, text := range failOn {
		r.failOn[text] = true
	}
	for _, text := range failOnce {
		r.failOnce[text] = true
	}
	for _, text := range gated {
		r.gated[text] = true
	}
	return r
}

// release opens the gate every gated turn is waiting on.
func (r *testRunner) release() { r.gateOnce.Do(func() { close(r.gate) }) }

func (r *testRunner) setFailOn(texts ...string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, text := range texts {
		r.failOn[text] = true
	}
}

// orderTexts is the texts that reached the runner, in the order they started, minus one.
func (r *testRunner) orderTexts(without string) []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	var texts []string
	for _, entry := range r.order {
		_, text, _ := strings.Cut(entry, "/")
		if text != without {
			texts = append(texts, text)
		}
	}
	return texts
}

func (r *testRunner) counts() (peak int, peakBy map[string]int, cancelled int, seen map[string]int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	peakBy = map[string]int{}
	for channel, value := range r.peakBy {
		peakBy[channel] = value
	}
	seen = map[string]int{}
	for text, value := range r.seen {
		seen[text] = value
	}
	return r.peak, peakBy, r.cancelled, seen
}

func (r *testRunner) run(ctx context.Context, job *Job) (string, error) {
	r.mu.Lock()
	r.order = append(r.order, job.Channel+"/"+job.Text)
	r.seen[job.Text]++
	r.active++
	if r.active > r.peak {
		r.peak = r.active
	}
	r.activeBy[job.Channel]++
	if r.activeBy[job.Channel] > r.peakBy[job.Channel] {
		r.peakBy[job.Channel] = r.activeBy[job.Channel]
	}
	shouldFail := r.failOn[job.Text] || (r.failOnce[job.Text] && r.seen[job.Text] == 1)
	gated, delay := r.gated[job.Text], r.delay
	r.mu.Unlock()

	defer func() {
		r.mu.Lock()
		r.active--
		r.activeBy[job.Channel]--
		r.mu.Unlock()
	}()

	if gated {
		select {
		case <-r.gate:
		case <-ctx.Done():
			return "", r.stopped()
		}
	} else {
		timer := time.NewTimer(delay)
		defer timer.Stop()
		select {
		case <-timer.C:
		case <-ctx.Done():
			return "", r.stopped()
		}
	}
	if shouldFail {
		return "", errors.New("RuntimeError: runner exploded")
	}
	return "echo:" + job.Text, nil
}

func (r *testRunner) stopped() error {
	r.mu.Lock()
	r.cancelled++
	r.mu.Unlock()
	return context.Canceled
}

// ------------------------------------------------------------------ helpers

type pending chan Outcome

func submitAsync(d *Dispatcher, job *Job, deliver func(string) error) pending {
	ch := make(chan Outcome, 1)
	go func() { ch <- d.Submit(job, deliver, true) }()
	return ch
}

func await(t *testing.T, p pending, what string) Outcome {
	t.Helper()
	select {
	case out := <-p:
		return out
	case <-time.After(30 * time.Second):
		t.Fatalf("timed out waiting for %s", what)
		return Outcome{}
	}
}

func waitUntil(t *testing.T, what string, ready func() bool) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if ready() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func waitIdle(t *testing.T, d *Dispatcher) {
	t.Helper()
	waitUntil(t, "the queue to go idle", func() bool {
		st := d.Status()
		return st.Queued == 0 && st.Running == 0 && st.Delivering == 0 && st.Retrying == 0
	})
}

func collector(sink *[]string) func(string) error {
	var mu sync.Mutex
	return func(text string) error {
		mu.Lock()
		defer mu.Unlock()
		*sink = append(*sink, text)
		return nil
	}
}

// answerLog records what each job was answered with, for the tests that do not hold the caller.
type answerLog struct {
	mu      sync.Mutex
	answers map[string]string
}

func (a *answerLog) deliver(job *Job) func(string) error {
	return func(text string) error {
		a.mu.Lock()
		defer a.mu.Unlock()
		if a.answers == nil {
			a.answers = map[string]string{}
		}
		a.answers[job.JobID] = text
		return nil
	}
}

func (a *answerLog) get(job *Job) string {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.answers[job.JobID]
}

func ptr[T any](value T) *T { return &value }

// ------------------------------------------------------------------ tests

func TestFairnessAndCaps(t *testing.T) {
	runner := newRunner(50*time.Millisecond, nil, nil, []string{"n0"})
	d := New(Config{
		Runner: runner.run, Concurrency: 2, MaxQueued: 8,
		ChannelLimits: map[string]int{"noisy": 1},
	})
	d.Start()
	defer d.Stop()

	answers := &answerLog{}
	var noisy []*Job
	for i := 0; i < 5; i++ {
		job := NewJob("noisy", "u", "ryan", fmt.Sprintf("n%d", i), fmt.Sprintf("m%d", i), "", 0)
		noisy = append(noisy, job)
		d.Submit(job, answers.deliver(job), false)
	}
	quiet := NewJob("quiet", "u", "ryan", "q1", "mq", "", 0)
	d.Submit(quiet, answers.deliver(quiet), false)

	// Limits are reported per channel while it has work: 1 for the capped one, the pool size for
	// an uncapped one.
	waitUntil(t, "both channels to have work", func() bool { return len(d.Status().Channels) == 2 })
	st := d.Status()
	if st.Channels["noisy"].Limit != 1 || st.Channels["quiet"].Limit != 2 {
		t.Fatalf("channel limits are %+v", st.Channels)
	}
	// n0 is held inside the runner, so the only thing the second worker may pick is the quiet
	// channel: the quiet turn runs second, not behind all five noisy ones.
	runner.release()
	waitIdle(t, d)

	for i, job := range noisy {
		if got := answers.get(job); got != fmt.Sprintf("echo:n%d", i) {
			t.Fatalf("turn %d answered %q", i, got)
		}
	}
	if got := answers.get(quiet); got != "echo:q1" {
		t.Fatalf("the quiet channel answered %q", got)
	}
	peak, peakBy, _, _ := runner.counts()
	if peak != 2 {
		t.Fatalf("pool cap breached: peak %d", peak)
	}
	if peakBy["noisy"] != 1 {
		t.Fatalf("channel cap breached: %v", peakBy)
	}
	runner.mu.Lock()
	order := append([]string{}, runner.order...)
	runner.mu.Unlock()
	if len(order) != 6 || order[0] != "noisy/n0" || order[1] != "quiet/q1" {
		t.Fatalf("run order was %v", order)
	}
}

func TestBackpressure(t *testing.T) {
	runner := newRunner(150*time.Millisecond, nil, nil, nil)
	const failedText = "Xử lý thất bại."
	d := New(Config{
		Runner: runner.run, Concurrency: 1, MaxQueued: 2, FailureText: failedText,
	})
	d.Start()
	defer d.Stop()

	first := submitAsync(d, NewJob("c", "u", "ryan", "j1", "m1", "", 0), nil)
	waitUntil(t, "the first turn to run", func() bool { return d.Status().Running == 1 })

	second := submitAsync(d, NewJob("c", "u", "ryan", "j2", "m2", "", 0), nil)
	third := submitAsync(d, NewJob("c", "u", "ryan", "j3", "m3", "", 0), nil)
	waitUntil(t, "the queue to fill", func() bool { return d.Status().Queued == 2 })

	refused := d.Submit(NewJob("c", "u", "ryan", "j4", "m4", "", 0), nil, false)
	if refused.Status != "rejected" || !strings.Contains(refused.Error, "full") {
		t.Fatalf("a full queue answered %+v", refused)
	}
	if d.Status().Rejected != 1 {
		t.Fatalf("the rejected counter is %d", d.Status().Rejected)
	}

	if out := await(t, first, "j1"); out.Reply != "echo:j1" {
		t.Fatalf("j1 answered %+v", out)
	}
	if out := await(t, second, "j2"); out.Reply != "echo:j2" {
		t.Fatalf("j2 answered %+v", out)
	}
	if out := await(t, third, "j3"); out.Reply != "echo:j3" {
		t.Fatalf("j3 answered %+v", out)
	}
	waitIdle(t, d)
	if texts := runner.orderTexts(""); strings.Contains(strings.Join(texts, ","), "j4") {
		t.Fatalf("the refused turn ran anyway: %v", texts)
	}

	// A turn that raises is reported as failed, and no reply is invented for it.
	runner.setFailOn("boom")
	outcome := await(t, submitAsync(d, NewJob("c", "u", "ryan", "boom", "m5", "", 0), nil), "boom")
	if outcome.Status != "failed" || outcome.Reply != "" {
		t.Fatalf("a failing turn answered %+v", outcome)
	}
	if !strings.Contains(outcome.Error, "RuntimeError") {
		t.Fatalf("a failing turn reported %q", outcome.Error)
	}
	if d.Status().Failed != 1 {
		t.Fatalf("the failed counter is %d", d.Status().Failed)
	}

	// A failing turn nobody is waiting for still answers its channel: the ack was already sent, so
	// silence here would leave the user with a promise and no answer.
	var answers []string
	acked := d.Submit(NewJob("c", "u", "ryan", "boom", "m6", "", 0), collector(&answers), false)
	if acked.Status != "queued" {
		t.Fatalf("the acked failing turn answered %+v", acked)
	}
	waitUntil(t, "the second failure", func() bool { return d.Status().Failed == 2 })
	waitIdle(t, d)
	if len(answers) != 1 || answers[0] != failedText {
		t.Fatalf("the channel was told %v", answers)
	}
}

func TestCancel(t *testing.T) {
	runner := newRunner(30*time.Second, nil, nil, nil)
	d := New(Config{Runner: runner.run, Concurrency: 1, MaxQueued: 4})
	d.Start()
	defer d.Stop()

	long := NewJob("c", "u", "ryan", "long", "m1", "", 0)
	running := submitAsync(d, long, nil)
	waitUntil(t, "the long turn to start", func() bool { return d.Status().Running == 1 })
	later := NewJob("c", "u", "ryan", "later", "m2", "", 0)
	waiting := submitAsync(d, later, nil)
	waitUntil(t, "the second turn to wait", func() bool { return d.Status().Queued == 1 })

	if stopped := d.Cancel(later.JobID); stopped.Status != "cancelled" {
		t.Fatalf("cancelling a queued turn answered %+v", stopped)
	}
	if out := await(t, waiting, "the cancelled waiter"); out.Status != "cancelled" {
		t.Fatalf("a cancelled waiter was told %+v", out)
	}
	if d.Status().Queued != 0 {
		t.Fatalf("the queue still holds %d jobs", d.Status().Queued)
	}

	if stopping := d.Cancel(long.JobID); stopping.Status != "cancelling" {
		t.Fatalf("cancelling a running turn answered %+v", stopping)
	}
	if out := await(t, running, "the cancelled turn"); out.Status != "cancelled" {
		t.Fatalf("a running turn reported %+v", out)
	}
	_, _, cancelled, _ := runner.counts()
	if cancelled != 1 {
		t.Fatalf("the runner was cancelled %d times", cancelled)
	}
	waitIdle(t, d)
	if d.Status().Cancelled != 2 {
		t.Fatalf("the cancelled counter is %d", d.Status().Cancelled)
	}
	if texts := runner.orderTexts(""); strings.Contains(strings.Join(texts, ","), "later") {
		t.Fatalf("the dropped turn ran: %v", texts)
	}
	if d.Cancel("nosuchjob").Status != "unknown" {
		t.Fatal("cancelling an unknown job did not answer unknown")
	}
}

func TestPriority(t *testing.T) {
	runner := newRunner(10*time.Millisecond, nil, nil, []string{"block"})
	d := New(Config{Runner: runner.run, Concurrency: 1, MaxQueued: 8})
	d.Start()
	defer d.Stop()

	blocker := NewJob("c", "u", "ryan", "block", "m0", "", 0)
	d.Submit(blocker, nil, false)
	waitUntil(t, "the blocking turn to run", func() bool { return d.Status().Running == 1 })

	low := NewJob("c", "u", "ryan", "low", "m1", "", 0)
	high := NewJob("c", "u", "ryan", "high", "m2", "", 5)
	mid := NewJob("c", "u", "ryan", "mid", "m3", "", 0)
	for _, job := range []*Job{low, high, mid} {
		if acked := d.Submit(job, nil, false); acked.Status != "queued" {
			t.Fatalf("a waiting turn was acked as %+v", acked)
		}
	}
	waitUntil(t, "three waiting turns", func() bool { return d.Status().Queued == 3 })

	d.Cancel(blocker.JobID)
	waitIdle(t, d)
	if st := d.Status(); st.Delivered != 3 {
		t.Fatalf("after the priority line the counters are %+v", st)
	}
	if order := runner.orderTexts("block"); strings.Join(order, ",") != "high,low,mid" {
		t.Fatalf("priority order was %v", order)
	}
}

func TestPriorityAcrossChannels(t *testing.T) {
	runner := newRunner(10*time.Millisecond, nil, nil, []string{"block"})
	d := New(Config{Runner: runner.run, Concurrency: 1, MaxQueued: 8})
	d.Start()
	defer d.Stop()

	blocker := NewJob("a", "u", "ryan", "block", "m0", "", 0)
	d.Submit(blocker, nil, false)
	waitUntil(t, "the blocking turn to run", func() bool { return d.Status().Running == 1 })

	// b is next in the rotation and would normally go first.
	for i, text := range []string{"n1", "n2"} {
		job := NewJob("b", "u", "ryan", text, fmt.Sprintf("mb%d", i), "", 0)
		d.Submit(job, nil, false)
	}
	urgent := NewJob("c", "u", "ryan", "urgent", "mc", "", 7)
	d.Submit(urgent, nil, false)
	waitUntil(t, "three waiting turns", func() bool { return d.Status().Queued == 3 })

	d.Cancel(blocker.JobID)
	waitIdle(t, d)
	if order := runner.orderTexts("block"); strings.Join(order, ",") != "urgent,n1,n2" {
		t.Fatalf("the rotation order was %v", order)
	}
}

func TestPriorityRespectsChannelLimit(t *testing.T) {
	runner := newRunner(10*time.Millisecond, nil, nil, []string{"hold", "free-1"})
	d := New(Config{
		Runner: runner.run, Concurrency: 3, MaxQueued: 8,
		ChannelLimits: map[string]int{"capped": 1},
	})
	d.Start()
	defer d.Stop()

	hold := submitAsync(d, NewJob("capped", "u", "ryan", "hold", "m0", "", 0), nil)
	waitUntil(t, "the capped channel's turn to run", func() bool { return d.Status().Running == 1 })
	urgent := submitAsync(d, NewJob("capped", "u", "ryan", "urgent", "m1", "", 9), nil)
	free := submitAsync(d, NewJob("free", "u", "ryan", "free-1", "m2", "", 0), nil)

	// The capped channel is at its limit, so its priority-9 head cannot start; the other channel's
	// ordinary turn takes the free slot instead, and the cap is never breached.
	waitUntil(t, "the second slot to fill", func() bool { return d.Status().Running == 2 })
	if texts := runner.orderTexts(""); strings.Contains(strings.Join(texts, ","), "urgent") {
		t.Fatalf("a priority head jumped its channel's cap: %v", texts)
	}
	_, peakBy, _, _ := runner.counts()
	if peakBy["capped"] != 1 {
		t.Fatalf("the capped channel peaked at %d", peakBy["capped"])
	}
	if limit := d.Status().Channels["capped"].Limit; limit != 1 {
		t.Fatalf("the capped channel reports limit %d", limit)
	}

	runner.release()
	if out := await(t, free, "free-1"); out.Reply != "echo:free-1" {
		t.Fatalf("free-1 answered %+v", out)
	}
	if out := await(t, hold, "hold"); out.Reply != "echo:hold" {
		t.Fatalf("hold answered %+v", out)
	}
	// Only once the capped channel's slot frees does its urgent head run.
	if out := await(t, urgent, "urgent"); out.Reply != "echo:urgent" {
		t.Fatalf("urgent answered %+v", out)
	}
	order := runner.orderTexts("")
	if indexOf(order, "urgent") < indexOf(order, "hold") {
		t.Fatalf("the priority head ran before the slot freed: %v", order)
	}
	_, peakBy, _, _ = runner.counts()
	if peakBy["capped"] != 1 {
		t.Fatalf("the capped channel peaked at %d", peakBy["capped"])
	}
}

func indexOf(values []string, want string) int {
	for i, value := range values {
		if value == want {
			return i
		}
	}
	return -1
}

func TestRetry(t *testing.T) {
	runner := newRunner(10*time.Millisecond, nil, []string{"flaky"}, nil)
	var delivered []string
	d := New(Config{
		Runner: runner.run, Concurrency: 1, MaxQueued: 4, FailureText: "failed",
		Retries: ptr(1), RetryDelay: ptr(0.2),
	})
	d.Start()
	defer d.Stop()

	flaky := NewJob("c", "u", "ryan", "flaky", "m1", "", 0)
	if acked := d.Submit(flaky, collector(&delivered), false); acked.Status != "queued" {
		t.Fatalf("the flaky turn was acked as %+v", acked)
	}
	// The retry waits out its backoff instead of hammering the runner immediately.
	waitUntil(t, "the backoff", func() bool { return d.Status().Retrying == 1 })
	_, _, _, seen := runner.counts()
	if seen["flaky"] != 1 {
		t.Fatalf("the flaky turn ran %d times before its retry", seen["flaky"])
	}
	waitUntil(t, "the retry to be delivered", func() bool { return d.Status().Delivered == 1 })
	waitIdle(t, d)
	if len(delivered) != 1 || delivered[0] != "echo:flaky" {
		t.Fatalf("the channel was told %v", delivered)
	}
	_, _, _, seen = runner.counts()
	if seen["flaky"] != 2 {
		t.Fatalf("the flaky turn ran %d times", seen["flaky"])
	}
	st := d.Status()
	if st.Retried != 1 || st.Failed != 0 {
		t.Fatalf("after one retry the counters are %+v", st)
	}
	if flaky.Attempts != 2 || flaky.Status != "done" {
		t.Fatalf("the flaky job ended %+v", flaky)
	}

	// With the retries spent, the failure is reported and the channel is told.
	var answers []string
	stubborn := Config{
		Runner:      newRunner(10*time.Millisecond, []string{"always"}, nil, nil).run,
		Concurrency: 1, MaxQueued: 4, FailureText: "boom-text",
		Retries: ptr(1), RetryDelay: ptr(0.05),
	}
	pool := New(stubborn)
	pool.Start()
	defer pool.Stop()
	pool.Submit(NewJob("c", "u", "ryan", "always", "m2", "", 0), collector(&answers), false)
	waitUntil(t, "the stubborn turn to fail", func() bool { return pool.Status().Failed == 1 })
	waitIdle(t, pool)
	if len(answers) != 1 || answers[0] != "boom-text" {
		t.Fatalf("the channel was told %v", answers)
	}
	if pool.Status().Retried != 1 {
		t.Fatalf("the stubborn turn was retried %d times", pool.Status().Retried)
	}
}

func TestWaiterSurvivesRetry(t *testing.T) {
	runner := newRunner(10*time.Millisecond, nil, []string{"flaky"}, nil)
	d := New(Config{
		Runner: runner.run, Concurrency: 1, MaxQueued: 4, FailureText: "failed",
		Retries: ptr(1), RetryDelay: ptr(0.3),
	})
	d.Start()
	defer d.Stop()

	waiting := submitAsync(d, NewJob("c", "u", "ryan", "flaky", "m1", "", 0), nil)
	waitUntil(t, "the backoff", func() bool { return d.Status().Retrying == 1 })

	// The backoff must not hold the only worker: with one slot, a second channel's turn has to run
	// while the failed one waits.
	other := await(t, submitAsync(d, NewJob("other", "u", "ryan", "during-backoff", "m2", "", 0), nil),
		"the turn during the backoff")
	if other.Reply != "echo:during-backoff" {
		t.Fatalf("the turn during the backoff answered %+v", other)
	}
	if d.Status().Running != 0 {
		t.Fatalf("a worker is still held: %+v", d.Status())
	}

	outcome := await(t, waiting, "the retried turn")
	if outcome.Status != "done" || outcome.Reply != "echo:flaky" {
		t.Fatalf("the waiting caller was answered %+v", outcome)
	}
	_, _, _, seen := runner.counts()
	if seen["flaky"] != 2 {
		t.Fatalf("the flaky turn ran %d times", seen["flaky"])
	}
	waitIdle(t, d)
}

func TestCancelChannel(t *testing.T) {
	runner := newRunner(10*time.Millisecond, nil, nil, []string{"n0", "n1", "w0", "w1"})
	d := New(Config{Runner: runner.run, Concurrency: 2, MaxQueued: 4})
	d.Start()
	defer d.Stop()

	var busy []pending
	for i := 0; i < 2; i++ {
		job := NewJob("noisy", "u", "ryan", fmt.Sprintf("n%d", i), fmt.Sprintf("m%d", i), "", 0)
		busy = append(busy, submitAsync(d, job, nil))
	}
	waitUntil(t, "both slots to be busy", func() bool { return d.Status().Running == 2 })
	var waiting []pending
	for i := 0; i < 2; i++ {
		job := NewJob("noisy", "u", "ryan", fmt.Sprintf("w%d", i), fmt.Sprintf("x%d", i), "", 0)
		waiting = append(waiting, submitAsync(d, job, nil))
	}
	other := submitAsync(d, NewJob("quiet", "u", "ryan", "q1", "mq", "", 0), nil)
	waitUntil(t, "three waiting turns", func() bool { return d.Status().Queued == 3 })

	queuedStopped, runningStopped := d.CancelChannel("noisy")
	if queuedStopped != 2 || runningStopped != 2 {
		t.Fatalf("cancel_channel stopped %d waiting and %d running", queuedStopped, runningStopped)
	}
	for _, p := range append(busy, waiting...) {
		if out := await(t, p, "a cancelled turn"); out.Status != "cancelled" {
			t.Fatalf("a cancelled turn ended %+v", out)
		}
	}
	if out := await(t, other, "the other channel"); out.Reply != "echo:q1" {
		t.Fatalf("the other channel answered %+v", out)
	}
	waitIdle(t, d)
	if d.Status().Cancelled != 4 {
		t.Fatalf("the cancelled counter is %d", d.Status().Cancelled)
	}
	if queued, _ := d.CancelChannel("nosuchchannel"); queued != 0 {
		t.Fatalf("cancelling an unknown channel stopped %d", queued)
	}
}

func TestRestartRecovers(t *testing.T) {
	root := t.TempDir()
	var delivered []string

	first := New(Config{
		Runner:  newRunner(30*time.Second, nil, nil, nil).run,
		RootDir: root, Concurrency: 1, MaxQueued: 4,
	})
	first.Start()
	first.Submit(NewJob("telegram", "42", "ryan", "gate-1", "m1", "", 0), collector(&delivered), false)
	waitUntil(t, "the first turn to run", func() bool { return first.Status().Running == 1 })
	queued := first.Submit(NewJob("telegram", "42", "ryan", "gate-2", "m2", "", 0),
		collector(&delivered), false)
	if queued.Status != "queued" {
		t.Fatalf("the second turn was acked as %+v", queued)
	}
	if _, err := os.Stat(filepath.Join(root, "memories", jobsFile)); err != nil {
		t.Fatalf("an accepted job must be on disk: %v", err)
	}
	first.Stop()

	// The next process has no callbacks left, so recovery delivers through the daemon's own route
	// back to the channel.
	var mu sync.Mutex
	var recovered [][3]string
	second := New(Config{
		Runner:  newRunner(10*time.Millisecond, nil, nil, nil).run,
		RootDir: root, Concurrency: 2,
		Deliver: func(job *Job, reply string) error {
			mu.Lock()
			defer mu.Unlock()
			recovered = append(recovered, [3]string{job.Channel, job.ReplyTarget(), reply})
			return nil
		},
	})
	second.Start()
	defer second.Stop()

	waitUntil(t, "the recovered jobs to be delivered", func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(recovered) >= 2
	})
	waitIdle(t, second)
	mu.Lock()
	defer mu.Unlock()
	if len(recovered) != 2 {
		t.Fatalf("recovered %v", recovered)
	}
	for _, entry := range recovered {
		if entry[0] != "telegram" || entry[1] != "42" {
			t.Fatalf("a recovered job went to %v", entry)
		}
		if !strings.HasPrefix(entry[2], "echo:gate-") {
			t.Fatalf("a recovered job was answered %q", entry[2])
		}
	}
}

func TestStopDuringBackoffKeepsJob(t *testing.T) {
	root := t.TempDir()
	var delivered []string

	first := New(Config{
		Runner:  newRunner(10*time.Millisecond, nil, []string{"flaky"}, nil).run,
		RootDir: root, Concurrency: 1, MaxQueued: 4,
		Retries: ptr(1), RetryDelay: ptr(30.0),
	})
	first.Start()
	first.Submit(NewJob("telegram", "42", "ryan", "flaky", "m1", "", 0), collector(&delivered), false)
	waitUntil(t, "the backoff", func() bool { return first.Status().Retrying == 1 })
	if len(delivered) != 0 {
		t.Fatalf("a reply was delivered during the backoff: %v", delivered)
	}
	first.Stop() // the process dies mid-backoff

	var mu sync.Mutex
	var recovered [][3]string
	second := New(Config{
		Runner:  newRunner(10*time.Millisecond, nil, nil, nil).run,
		RootDir: root, Concurrency: 1,
		Deliver: func(job *Job, reply string) error {
			mu.Lock()
			defer mu.Unlock()
			recovered = append(recovered, [3]string{job.Channel, job.ReplyTarget(), reply})
			return nil
		},
	})
	second.Start()
	defer second.Stop()

	waitUntil(t, "the recovered job", func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(recovered) > 0
	})
	waitIdle(t, second)
	mu.Lock()
	defer mu.Unlock()
	if len(recovered) != 1 || recovered[0] != [3]string{"telegram", "42", "echo:flaky"} {
		t.Fatalf("recovered %v", recovered)
	}
}
