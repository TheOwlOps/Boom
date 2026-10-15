package agentrpc

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	_ "modernc.org/sqlite"
)

// fakeModel speaks the OpenAI wire format on loopback and remembers what it was sent, so a test
// can prove which messages reached the model. Only the model is fake: the worker is the real
// Python brain, spawned as a process.
type fakeModel struct {
	mu       sync.Mutex
	requests int
	messages []int
	delay    time.Duration
	content  string
}

func (f *fakeModel) handler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Messages []struct {
				Role string `json:"role"`
			} `json:"messages"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		f.mu.Lock()
		f.requests++
		f.messages = append(f.messages, len(body.Messages))
		delay, content := f.delay, f.content
		f.mu.Unlock()
		if delay > 0 {
			time.Sleep(delay)
		}
		w.Header().Set("content-type", "application/json")
		fmt.Fprintf(w, `{"id":"chatcmpl-fake","choices":[{"message":{"role":"assistant",`+
			`"content":%q},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,`+
			`"completion_tokens":1,"total_tokens":2}}`, content)
	}
}

func pythonCommand() []string {
	if custom := strings.Fields(os.Getenv("BOOM_PYTHON")); len(custom) > 0 {
		return custom
	}
	if runtime.GOOS == "windows" {
		return []string{"py", "-3"}
	}
	return []string{"python3"}
}

func repoRoot(t *testing.T) string {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("cannot locate this test file")
	}
	return filepath.Dir(filepath.Dir(filepath.Dir(file)))
}

func TestWorkerAndPool(t *testing.T) {
	py := pythonCommand()
	if _, err := exec.LookPath(py[0]); err != nil {
		t.Skipf("no Python interpreter (%s): %v", py[0], err)
	}
	fake := &fakeModel{content: "pong from the fake model"}
	model := httptest.NewServer(fake.handler())
	defer model.Close()

	work := t.TempDir()
	cfg := Config{
		Command: py,
		Script:  filepath.Join(repoRoot(t), "agent", "src", "cli.py"),
		Dir:     work,
		Env: []string{
			"OPENAI_API_KEY=test-key",
			"OPENAI_BASE_URL=" + model.URL + "/v1",
			"BOOM_MODEL=fake-model",
			"BOOM_YOLO=1",
		},
		Log:     os.Stderr,
		Timeout: 120 * time.Second,
	}

	pool, err := New(cfg, 2)
	if err != nil {
		t.Fatalf("start pool: %v", err)
	}
	defer pool.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 240*time.Second)
	defer cancel()

	modelName, err := pool.Ping(ctx)
	if err != nil {
		t.Fatalf("ping: %v", err)
	}
	if modelName != "fake-model" {
		t.Fatalf("ping reported model %q, want fake-model", modelName)
	}

	res, err := pool.Turn(ctx, TurnRequest{
		SessionID: "edge-s1", Channel: "telegram", SenderName: "ryan", Text: "ping"})
	if err != nil {
		t.Fatalf("turn: %v", err)
	}
	if res.Action != "pong from the fake model" || !res.Success {
		t.Fatalf("turn result %+v", res)
	}

	// A second turn in the same session must reach the model with the first one behind it: the
	// session keeps its worker (sticky), so the live context is the fuller one.
	if _, err := pool.Turn(ctx, TurnRequest{
		SessionID: "edge-s1", Channel: "telegram", Text: "again"}); err != nil {
		t.Fatalf("second turn: %v", err)
	}
	fake.mu.Lock()
	counts, requests := append([]int{}, fake.messages...), fake.requests
	fake.mu.Unlock()
	if requests != 2 {
		t.Fatalf("model saw %d requests, want 2", requests)
	}
	if counts[1] <= counts[0] {
		t.Fatalf("second turn sent %d messages, first sent %d: the session context was lost",
			counts[1], counts[0])
	}
	assertTranscript(t, filepath.Join(work, "state.db"), "edge-s1")

	// A turn that outlives its timeout kills its worker and is reported as failed; the pool puts a
	// fresh process in its place, so the next turn runs instead of the pool hanging.
	fake.mu.Lock()
	fake.delay = 8 * time.Second
	fake.mu.Unlock()
	slow := cfg
	slow.Timeout = 5 * time.Second
	small, err := New(slow, 1)
	if err != nil {
		t.Fatalf("start timeout pool: %v", err)
	}
	defer small.Close()
	if _, err := small.Turn(ctx, TurnRequest{SessionID: "edge-slow", Text: "slow"}); err == nil {
		t.Fatal("a turn that outlived its timeout was reported as success")
	} else if !strings.Contains(err.Error(), "deadline exceeded") {
		t.Fatalf("timeout turn failed with %v, want a deadline error", err)
	}
	fake.mu.Lock()
	fake.delay = 0
	fake.mu.Unlock()
	if alive := small.Alive(); alive != 1 {
		t.Fatalf("pool has %d live workers after a timeout, want the dead one replaced", alive)
	}
	if _, err := small.Turn(ctx, TurnRequest{SessionID: "edge-slow", Text: "after restart"}); err != nil {
		t.Fatalf("turn after the worker was replaced: %v", err)
	}
}

func assertTranscript(t *testing.T, dbPath, session string) {
	t.Helper()
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("open %s: %v", dbPath, err)
	}
	defer db.Close()
	rows, err := db.Query(
		"SELECT role FROM messages WHERE session_id = ? ORDER BY id", session)
	if err != nil {
		t.Fatalf("read transcript: %v", err)
	}
	defer rows.Close()
	var roles []string
	for rows.Next() {
		var role string
		if err := rows.Scan(&role); err != nil {
			t.Fatalf("scan transcript: %v", err)
		}
		roles = append(roles, role)
	}
	answered := false
	for _, role := range roles {
		if role == "assistant" {
			answered = true
		}
	}
	if len(roles) < 2 || roles[0] != "user" || !answered {
		t.Fatalf("session %s transcript is %v, want a user turn and an answer", session, roles)
	}
}
