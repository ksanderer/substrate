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

package main

import (
	"bytes"
	"crypto/sha256"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"time"
)

const workdir = "/workspace"

func main() {
	if err := os.MkdirAll(workdir, 0755); err != nil {
		log.Fatal(err)
	}
	for i := range 500 {
		line := fmt.Appendf(nil, "fixture %04d\n", i)
		if err := os.WriteFile(name(i), bytes.Repeat(line, 256), 0644); err != nil {
			log.Fatal(err)
		}
	}
	http.HandleFunc("/readyz", func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, "ok\n")
	})
	http.HandleFunc("/fs", func(w http.ResponseWriter, r *http.Request) {
		i, _ := strconv.Atoi(r.URL.Query().Get("i"))
		i %= 500
		var err error
		var n int
		switch r.URL.Query().Get("op") {
		case "read":
			var data []byte
			data, err = os.ReadFile(name(i))
			n = len(data)
		case "write":
			data := bytes.Repeat([]byte("edited fixture\n"), 256)
			var file *os.File
			file, err = os.CreateTemp(workdir, ".edit-")
			if err == nil {
				defer os.Remove(file.Name())
				n, err = file.Write(data)
				closeErr := file.Close()
				if err == nil {
					err = closeErr
				}
				if err == nil {
					err = os.Rename(file.Name(), name(i))
				}
			}
		case "list":
			var items []string
			items, err = filepath.Glob(filepath.Join(workdir, "*.txt"))
			n = len(items)
		case "search":
			for i := range 500 {
				var data []byte
				data, err = os.ReadFile(name(i))
				if err != nil {
					break
				}
				if bytes.Contains(data, []byte("fixture")) {
					n++
				}
			}
		default:
			http.Error(w, "unknown op", 400)
			return
		}
		if err != nil {
			http.Error(w, err.Error(), 500)
			return
		}
		fmt.Fprintln(w, n)
	})
	http.HandleFunc("/burst", func(w http.ResponseWriter, r *http.Request) {
		mib, err := strconv.Atoi(r.URL.Query().Get("mib"))
		if err != nil || mib < 0 || mib > 1024 {
			http.Error(w, "mib out of range for this benchmark", 400)
			return
		}
		ms, err := strconv.Atoi(r.URL.Query().Get("ms"))
		if err != nil || ms < 0 || ms > 60000 {
			http.Error(w, "ms out of range for this benchmark", 400)
			return
		}
		memory := make([]byte, mib<<20)
		for i := 0; i < len(memory); i += os.Getpagesize() {
			memory[i] = byte(i / os.Getpagesize())
		}
		block := make([]byte, 64<<10)
		deadline := time.Now().Add(time.Duration(ms) * time.Millisecond)
		for time.Now().Before(deadline) {
			sum := sha256.Sum256(block)
			copy(block, sum[:])
		}
		if hold := r.URL.Query().Get("hold"); hold != "" {
			duration, err := time.ParseDuration(hold)
			if err != nil || duration < 0 || duration > time.Minute {
				http.Error(w, "hold out of range for this benchmark", 400)
				return
			}
			time.Sleep(duration)
		}
		runtime.KeepAlive(memory)
		fmt.Fprintln(w, len(memory), block[0])
	})
	log.Fatal(http.ListenAndServe(":80", nil))
}

func name(i int) string {
	return filepath.Join(workdir, fmt.Sprintf("%04d.txt", i))
}
