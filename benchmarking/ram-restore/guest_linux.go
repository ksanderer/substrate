// Copyright 2026 The Agent Substrate Authors
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// The benchmark guest runs as init and exposes its in-memory payload over vsock.
package main

import (
	"bufio"
	"crypto/sha256"
	"fmt"
	"log"
	"os"

	"golang.org/x/sys/unix"
)

const (
	payloadSize = 64 << 20
	port        = 9000
)

func main() {
	payload := make([]byte, payloadSize)
	page := os.Getpagesize()
	for offset := 0; offset < len(payload); offset += page {
		payload[offset] = byte(offset/page%255 + 1)
	}

	listener, err := unix.Socket(unix.AF_VSOCK, unix.SOCK_STREAM|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		log.Fatal(err)
	}
	if err := unix.Bind(listener, &unix.SockaddrVM{CID: unix.VMADDR_CID_ANY, Port: port}); err != nil {
		log.Fatal(err)
	}
	if err := unix.Listen(listener, unix.SOMAXCONN); err != nil {
		log.Fatal(err)
	}

	for {
		fd, _, err := unix.Accept4(listener, unix.SOCK_CLOEXEC)
		if err == unix.EINTR {
			continue
		}
		if err != nil {
			log.Fatal(err)
		}
		handle(os.NewFile(uintptr(fd), "vsock"), payload, page)
	}
}

func handle(conn *os.File, payload []byte, page int) {
	defer conn.Close()
	command, err := bufio.NewReader(conn).ReadString('\n')
	if err != nil {
		return
	}
	switch command {
	case "ping\n":
		fmt.Fprintln(conn, "ready")
	case "checksum\n":
		fmt.Fprintf(conn, "%x\n", sha256.Sum256(payload))
	case "mutate\n":
		for offset := 0; offset < len(payload)/2; offset += page {
			payload[offset] ^= 0xff
		}
		fmt.Fprintf(conn, "%x\n", sha256.Sum256(payload))
	default:
		fmt.Fprintln(conn, "unknown command")
	}
}
