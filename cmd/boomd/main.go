// boomd is the Go edge of the Boom harness. It owns what faces the outside world — the webhook
// ingress, the channel clients, the turn queue and the delivery ledger — and drives the Python
// brain (the ReAct loop and its tools) over the line protocol in internal/agentrpc.
package main

import (
	"fmt"
	"os"
)

const version = "0.1.0"

func main() {
	if len(os.Args) > 1 && (os.Args[1] == "--version" || os.Args[1] == "-v") {
		fmt.Println("boomd", version)
		return
	}
	fmt.Fprintf(os.Stderr, "boomd %s: edge not wired yet — channels, queue and the agent pool "+
		"land in the next steps; nothing is listening on any port\n", version)
	os.Exit(1)
}
