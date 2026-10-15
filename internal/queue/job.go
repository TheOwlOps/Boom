// Package queue runs agent turns for the channels: a bounded, durable, per-channel queue, so one
// heavy turn cannot stall a channel and a message that was accepted survives a restart.
//
// It is the Go side of the contract `gateway/dispatcher.py` established: the same env knobs, the
// same counters, and `memories/gateway_jobs.json` in the same shape, so a queue written by either
// edge is readable by the other while the two run side by side.
package queue

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"time"
)

const (
	jobsFile        = "gateway_jobs.json"
	maxKept         = 200 // finished records kept for the operator, dropped oldest-first
	maxPriority     = 9
	defaultPriority = 0

	statusQueued    = "queued"
	statusRunning   = "running"
	statusDone      = "done"
	statusFailed    = "failed"
	statusCancelled = "cancelled"
)

// Job is one queued turn. The JSON names are the Python ones, so both edges read the same file.
type Job struct {
	JobID      string `json:"job_id"`
	Channel    string `json:"channel"`
	SenderID   string `json:"sender_id"`
	SenderName string `json:"sender_name"`
	Text       string `json:"text"`
	MessageID  string `json:"message_id"`
	// Target is where the reply goes when that is not the sender: a Discord guild message is
	// answered in the channel it arrived on, not to the author.
	Target    string  `json:"target"`
	Priority  int     `json:"priority"`
	CreatedAt float64 `json:"created_at"`
	Attempts  int     `json:"attempts"`
	Status    string  `json:"status"`
}

// ReplyTarget is the id a reply is sent to.
func (j *Job) ReplyTarget() string {
	if j.Target != "" {
		return j.Target
	}
	return j.SenderID
}

// NewJob builds a queued turn with the id and clamping the caller should not have to think about.
func NewJob(channel, senderID, senderName, text, messageID, target string, priority int) *Job {
	if priority < 0 {
		priority = defaultPriority
	} else if priority > maxPriority {
		priority = maxPriority
	}
	return &Job{
		JobID:      newID(),
		Channel:    channel,
		SenderID:   senderID,
		SenderName: senderName,
		Text:       text,
		MessageID:  messageID,
		Target:     target,
		Priority:   priority,
		CreatedAt:  float64(time.Now().UnixNano()) / 1e9,
		Status:     statusQueued,
	}
}

func newID() string {
	buf := make([]byte, 6)
	if _, err := rand.Read(buf); err != nil {
		return strconv.FormatInt(time.Now().UnixNano(), 16)
	}
	return hex.EncodeToString(buf)
}

// jobsPath is where the queue lives, or "" when the dispatcher was given no root directory.
func jobsPath(rootDir string) string {
	if rootDir == "" {
		return ""
	}
	return filepath.Join(rootDir, "memories", jobsFile)
}

// loadJobs reads back the jobs an earlier run left behind. A job that was mid-flight when the
// process died is re-queued, not assumed done: the reply was never sent.
func loadJobs(path string) []*Job {
	if path == "" {
		return nil
	}
	blob, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var file struct {
		Unfinished []*Job `json:"unfinished"`
	}
	if err := json.Unmarshal(blob, &file); err != nil {
		return nil
	}
	recovered := make([]*Job, 0, len(file.Unfinished))
	for _, job := range file.Unfinished {
		if job == nil || job.JobID == "" {
			continue
		}
		job.Status = statusQueued
		recovered = append(recovered, job)
	}
	return recovered
}

// saveJobs writes the queue atomically: a killed daemon must never leave a half-written file.
func saveJobs(path string, jobs []*Job) error {
	if path == "" {
		return nil
	}
	var unfinished, finished []*Job
	for _, job := range jobs {
		switch job.Status {
		case statusQueued, statusRunning:
			unfinished = append(unfinished, job)
		case statusDone, statusFailed, statusCancelled:
			finished = append(finished, job)
		}
	}
	if len(finished) > maxKept {
		finished = finished[len(finished)-maxKept:]
	}
	blob, err := json.MarshalIndent(map[string]any{
		"unfinished": unfinished, "finished": finished,
	}, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, blob, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
