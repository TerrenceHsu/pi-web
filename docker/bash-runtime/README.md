# Local Bash runtime (stage 1, not enabled in Web)

The backend passed 14 real Docker checks on 2026-09-06. Internal ExecutionGrant
primitives exist, but `run_bash`, shared Coding/Plan/Web approval wiring and
publication integration are not yet implemented. Web execution stays disabled.
No image is pulled and no container is created by installing/importing the package.

## Prepare an image explicitly

Use an already-approved, locally available immutable Python slim base image.
Build from the repository root with `docker/bash-runtime/Dockerfile`, supplying
that base via `BASE_IMAGE`, `--network=none` and `--pull=false`. This step requires
an operator; it is never performed by a chat request. The resulting runtime's
full `sha256:…` image ID, not its mutable tag, is used in runtime configuration.

The Dockerfile-specific ignore file sends only the worker source. `--network=none`
isolates build **RUN instructions**, not BuildKit's registry metadata resolution.
The initial build used existing base layers but requested registry metadata/auth;
it was not strictly offline. Do not promise an air-gapped build from these flags.
See [Docker network scope](https://docs.docker.com/reference/dockerfile/#run---network).

The image contains Bash, Python and the stdlib-only transfer helper. Its inert
PID 1 runs as UID 10000; task commands and transfers run as UID 10001. Both are
unprivileged. Different UIDs prevent user commands from taking over the supervisor.
No Web/Provider/MCP code, credentials or host Workspace mount is included.
The execution policy requires Linux cgroup v2, private cgroup namespace, runc,
seccomp, no IPC shared memory and no exec-child OOM events before reuse.

## Check without enabling

Use project Python with `PYTHONPATH=src` to run `scripts/check_bash_docker.py`.
Arguments:

- `--docker-executable`: absolute path, needed when Docker is not on PATH.
- `--image-id`: full immutable ID of the locally built runtime.
- `--verify`: explicitly create temporary probe containers and validate task
  reuse, file transfer, network/user/environment policy, one-way freeze and
  rejection/cleanup of a surviving background process.
  Extended checks cover kernel limits, temporary-disk ENOSPC, bounded combined
  output, failed-command repair, timeout, cancellation, detached processes, OOM,
  symlink/hardlink/FIFO rejection and verified cleanup before finalizers run.

Without `--verify`, only daemon/image metadata is inspected; it does not prove
isolation, and the feature remains unavailable. Exit code 2 means missing
prerequisites or incomplete verification, not a successful runtime test.
Even successful backend verification does not enable Web execution: the later
grant, authorization, artifact and UI stages must also be completed.

## tmpfs export boundary

Docker documents limitations copying tmpfs using `docker cp`. This adapter does
not use that operation. It pauses the container, requires the process list to
contain only the known inert PID 1, then allows only the image-owned stdlib reader
to stream the bytes. The instance returns to paused state. A frozen operation
never permits user `execute` or upload again. If quiescence cannot be established,
the entire container is destroyed. There is no writable host-directory fallback.

The supervisor's sleep expires a running container, but a paused container cannot
schedule its own exit. Independent TTL/orphan reconciliation and restart cleanup
are required product integration gates, not guarantees provided by PID 1 alone.

The returned tar is **untrusted** until the publication layer validates all paths,
members, sizes and hashes. The backend does not extract or publish it to Workspace.
See [Docker cp limitations](https://docs.docker.com/reference/cli/docker/container/cp/).
