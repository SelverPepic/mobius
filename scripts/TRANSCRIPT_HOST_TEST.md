# Private transcript Host cutover readiness

This is a **constrained preparation**, not a passed disposable-host cutover.
Do not run it on `/data/platform`, a production host, or any host with existing
Möbius data. No image is published by this procedure.

## What can run now

On an isolated test checkout, run
`scripts/wt-pytest.sh backend/tests/test_transcript_host_cutover.py -q`.
It constructs a disposable legacy SQLite database, runs the actual level-1
transcript gate, compares both normalized messages and the byte-exact preserved
legacy archive, executes the Host's exact floor probe, and checks that its
rollback policy refuses a level-0 image after activation without Compose `up`.
The Docker image probes and app fencing are simulated; this is **not** evidence
that a real worker, mounted volume, or systemd unit works.

On a **fresh disposable systemd/Docker host**, the read-only preflight is
`scripts/test-transcript-host-cutover.sh <candidate-full-sha> <local-image>`.
It requires a local image with matching revision/source labels and refuses the
production container, volume, and helper paths if already present. It performs
no install, replacement, rollback, or cleanup. A pass is not deployment proof.

## ARM64 private-image validation recipe (no publish)

Use a disposable ARM64 machine with an empty Docker data root, private clone,
and an isolated, throwaway disk/volume. From the **candidate commit**:

```sh
SHA=$(git rev-parse HEAD)
DATE=$(git show -s --format=%cs HEAD)
docker buildx build --platform linux/arm64 --load \
  --build-arg BUILD_SHA="$SHA" --build-arg BUILD_DATE="$DATE" \
  -t "mobius-transcript-private:sha-$SHA" .
scripts/test-transcript-host-cutover.sh "$SHA" "mobius-transcript-private:sha-$SHA"
scripts/wt-pytest.sh backend/tests/test_transcript_host_cutover.py -q
```

These commands validate the **local ARM64 image identity and isolated policy
test only**. They do not invoke a public workflow or push. A local `docker
compose up` of the candidate on a disposable database can additionally probe
its real entrypoint/readiness, but it cannot by itself prove Host replacement,
pre-activation rollback, or exact active-chat handoff. Save before/after SQL
evidence for `chat_messages` in `seq` order, `upgrade_archive` verified by
`one_way_upgrades.verified_legacy_copy`, `platform_compat.floor`, and the
running container image ID; never score only JSON-normalized content as exact
archive survival.

## Missing end-to-end proof / go-no-go

`test-host-helper.sh` cannot be reused unchanged: it pulls **both** public
SHA images and expects the reverse replacement to succeed. The installed
worker in `mobius-rebuild-host.py` hardcodes `ghcr.io/mobius-os/mobius`, calls
`docker pull`, requires the official source/revision labels **and amd64**, and
the installer rejects ARM64. Merely retagging a local candidate does not make
the real worker accept it. Do not patch or bypass those checks to call it an
end-to-end pass. A private, reviewed test-only image-routing mechanism (or
approved private registry with equivalent identity checks) and an amd64
disposable systemd host are prerequisites for the real worker test. ARM64
Host-worker proof additionally needs explicit architecture support in the
production installer/worker; this task does not change them.

When those prerequisites exist, the host test must: start a level-0 image on
fresh data; create an owner/service token and noncanonical duplicate-ID chat
fixture; install the helper from that image's checkout; request the level-1
image through the app inbox; verify the **container ID changed**, served SHA,
worker adoption, exact `chat_messages` order and archive bytes, and floor 1;
then, in a **separate fresh fixture**, inject a candidate failure *after its
gate activates but before the worker declares health* and prove the actual
worker reports `needs_recovery/newer_version_required` without starting the
old image. Another fresh fixture must fail **before activation** and show
the old image restored, floor 0, and exact transcript retained. Do not use the
old harness's unconditional return-to-previous assertion.

Record elapsed times against the deployment's **120-second preflight and
120-second cutover defaults** (`deploy-prod.sh`); do not enlarge them silently.
The worker's health wait is 180 seconds and rollback readiness wait is 120
seconds, which are distinct bounds, not permission to relax deploy deadlines.
