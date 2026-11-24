#!/bin/sh
# Copyright 2026 The Agent Substrate Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -eu

if [ "$#" -ne 2 ]; then
    printf 'Usage: %s <guest-binary> <output.cpio>\n' "$0" >&2
    exit 2
fi

out=$(realpath -m "$2")
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT

install -m 0755 "$1" "$stage/init"
mkdir "$stage/dev"
mknod -m 0600 "$stage/dev/console" c 5 1

(
    cd "$stage"
    printf '%s\n' init dev dev/console | cpio --quiet -o -H newc
) > "$out"
