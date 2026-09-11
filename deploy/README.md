# Demo host: pull-based deploy

What runs the public demo VM. The host pulls `origin/main` every two minutes
and brings itself up to it; nothing pushes to it, and no CI job holds a
credential for it. `infra/` is separate - that is the Terraform for the
container deployment, not this box.

These files lived only on the VM until 2026-09-10, which meant three fixes
made that evening existed nowhere else. If the host is ever rebuilt, copy them
back:

    sudo install -m 755 deploy/deploy.sh /opt/miso/deploy.sh
    sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now miso-backend.service miso-deploy.timer

`deploy.sh` is NOT self-updating. It is the thing running the pull, so a pull
that rewrote it mid-run would be replacing the script under its own feet.
Editing it here changes what a rebuilt host gets; the running host keeps what
is in `/opt/miso/`. After changing it, install it by hand with the line above.

## Three things it has to get right

**A checkout that fails must stop the deploy.** `data/logs/requests.jsonl` was
tracked and the app rewrites it on every request, so `git checkout` aborted -
and the script carried on to rebuild the frontend, restart the backend and log
"healthy" while the code never moved. The host silently stopped deploying and
nothing said so. The log file is gitignored now, and the checkout is guarded
either way.

**The backend must come back no matter what fails.** The poller exits 2 when
the rate guard skips every endpoint, which is normal right after a scheduled
cycle. That exit killed the script between `systemctl stop` and `systemctl
start`, and the next run exited early because the commit already matched - so
nothing ever restarted it. A `trap ... EXIT` now guarantees the start.

**Health is polled, not slept on.** Boot loads the embedding model, about
twenty seconds on this box, so a flat 12-second sleep reported every good
deploy as a failure. An alarm that cries wolf is how a real one gets ignored.

## Order matters

Chroma has one writer. The script stops the backend before the poller and the
ingest touch the store, and starts it afterwards. Running
`python -m backend.poller` by hand while the backend is up poisons the running
process's Chroma client until it is restarted - every question after that
returns an error. That is why the deploy does it in this order, and why you
should let the deploy do it rather than running the steps yourself.
