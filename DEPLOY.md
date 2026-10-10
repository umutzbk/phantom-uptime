# Deployment

One small VM, three containers, one URL. The deployment is meant to be boring:
the interesting part is the product, not the infrastructure.

The reference setup is a single AWS EC2 instance, but nothing here is AWS
specific beyond the bootstrap script. Any Linux host with Docker works.

---

## What is running

| Piece | Where | Notes |
|---|---|---|
| Caddy | container `phantom-caddy`, ports 80 and 443 | terminates TLS, the only thing exposed |
| FastAPI | container `phantom-api`, port 8000 on loopback | serves the UI and the API |
| Cube | container `phantom-cube`, port 4000 on loopback | reachable from the API over the compose network |
| Marts | `marts/*.parquet` | mounted read only into Cube |
| Traces | baked into the image at `/app/demo_traces` | replay works on a fresh host |

A `t3.small` (2 GB) is enough. About 15 US dollars a month if left running.

### A hostname without owning a domain

`sslip.io` resolves `203-0-113-10.sslip.io` to `203.0.113.10`, so Caddy can obtain a
real certificate for a bare IP address. That matters for more than tidiness: a
browser will not let an https page call an http backend, so without a
certificate a separately hosted frontend could never reach this one.

---

## From nothing to a running instance

### 1. Instance

* Amazon Linux 2023, `t3.small`, 20 GB gp3.
* Security group: 22 from your address only, 80 and 443 from anywhere.
  80 is not optional; certificate renewal needs it.
* User data: `scripts/ec2_user_data.sh` installs Docker and the compose plugin
  and creates `/opt/copilot`.
* Attach an Elastic IP. Without one the address changes on every stop.

### 2. Bundle

Deployment copies a tarball rather than cloning, so the host needs no git access.

```bash
python scripts/make_deploy_bundle.py          # -> dist/copilot-deploy.tar.gz, about 10 MB
scp -i <key.pem> dist/copilot-deploy.tar.gz ec2-user@<ip>:/opt/copilot/
ssh -i <key.pem> ec2-user@<ip> 'cd /opt/copilot && tar xzf copilot-deploy.tar.gz && rm copilot-deploy.tar.gz'
```

Raw extracts and the DuckDB warehouse stay local. Cube reads the parquet marts
and nothing else needs them.

### 3. Environment

```bash
ssh -i <key.pem> ec2-user@<ip>
cd /opt/copilot
cp .env.production.example .env
nano .env          # see the table below
chmod 600 .env
```

| Variable | What it does | If you get it wrong |
|---|---|---|
| `DEMO_USER`, `DEMO_PASSWORD` | the login gate | blank means no gate at all, and the site is public |
| `SESSION_SECRET` | signs the session cookie | blank means a new random one per restart, so everyone is logged out |
| `COOKIE_SECURE` | `1` behind https | `1` on plain http and the browser silently drops the cookie, so login appears to do nothing |
| `ANTHROPIC_API_KEY` | live mode | blank still works: the app serves recorded investigations and says so |
| `COPILOT_MODE` | `replay` or `live` | `replay` is the safe default for a public URL |
| `CUBEJS_API_SECRET` | Cube signs and verifies with this | must match on both services, which compose already arranges |
| `SITE_ADDRESS` | the hostname Caddy answers on | wrong hostname means no certificate |
| `CORS_ORIGINS` | only for a frontend hosted elsewhere | leave blank for a single-origin deployment |

### 4. Up

```bash
cd /opt/copilot && docker compose up -d --build
docker compose ps
curl -s localhost:8000/health
```

Expect `"status":"ok"` with `cube.reachable: true`. First boot takes a couple of
minutes: Cube pulls about a gigabyte.

### 5. Redeploy after a change

```bash
python scripts/make_deploy_bundle.py
scp -i <key.pem> dist/copilot-deploy.tar.gz ec2-user@<ip>:/opt/copilot/
ssh -i <key.pem> ec2-user@<ip> \
  'cd /opt/copilot && tar xzf copilot-deploy.tar.gz && rm copilot-deploy.tar.gz && docker compose up -d --build'
```

A change to the Cube model only needs `docker compose restart cube`.

---

## Five things that only broke in production

Every one of these passed locally and failed on the server. They are written
down because the pattern matters more than the fixes: development defaults hide
production behaviour.

1. **The Cube healthcheck could never pass.** It used `curl`, and the image ships
   neither `curl` nor `wget`. The container sat permanently unhealthy, and
   because the API waited for `service_healthy` it would never have started. The
   check now uses the node that is already in the image, and the API waits only
   for `service_started`: replay mode needs no semantic layer, so a sick Cube
   should degrade the product, not prevent it.

2. **Cube demanded a token.** With `CUBEJS_DEV_MODE=false` every request needs a
   signed JWT. The client now signs one with the shared secret, which is fifteen
   lines and avoids running the semantic layer wide open.

3. **Cube demanded a Cube Store.** Production mode expects a separate queue and
   cache service. One box, one process: `CUBEJS_CACHE_AND_QUEUE_DRIVER=memory`
   is the honest answer, and it saves a fourth container on a 2 GB instance.

4. **Primary keys are private in production.** `silent_failures.charger_id` is a
   primary key, and dev mode had been quietly allowing it to be queried. It is
   now `public: true` in the model, which is what the tools were relying on all
   along.

5. **Hardening for production broke development.** Closing Cube's published port
   was right for the server and wrong for the laptop: the tools could no longer
   be called directly, and the offline gate silently lost its source of truth.
   It is now published on `127.0.0.1` only, which is correct in both places.

A sixth, less interesting: the SDK pin was `anthropic==0.69.0`, which has no
`output_config`, so live mode failed on the server while working on the laptop.
Pinned to `1.4.0`.

---

## Smoke test

Run this after any redeploy.

```bash
B=https://<your-host>

curl -s $B/health                                  # status ok, cube reachable
curl -s -o /dev/null -w '%{http_code}\n' $B/        # 401, the gate is on
curl -s -c ck.txt -X POST -F username=<user> -F password=<password> $B/login   # 303
curl -s -b ck.txt $B/api/dashboard | head -c 200    # 99.26 / 95.29 / 123 / 26 / 0
curl -s -b ck.txt $B/api/questions | head -c 120    # 8 questions, 8 with traces
```

Then in a browser, signed in:

- [ ] Pilot overview renders: 123 alerts, 26 silent failures, no overlap
- [ ] The table lists CHG-0034 at Birmingham Motorway 006 first
- [ ] Replay: ask "Why CHG-0034?" and the answer streams in
- [ ] The evidence card shows 63 attempts, 0 successful, 100% reported uptime
- [ ] "How this answer was produced" lists the approved tool calls
- [ ] The figure check under the answer says all figures matched
- [ ] Live: switch the toggle, ask again, watch the tools fire in order
- [ ] Draft an operations action: the email and ticket appear, nothing is sent

---

## Optional: a Vercel hostname

`vercel.json` rewrites every path to the backend (replace `YOUR-BACKEND-HOST`).
Be clear about what that buys: a nicer hostname, and nothing else. Vercel serves
none of the application itself, because if it served the UI from its own origin
the login gate would sit on the backend while the page came from Vercel, and the
shell would load for anyone. Proxying everything keeps one gate and keeps the
session cookie first-party, which matters because browsers now block third-party
cookies by default.

```bash
npm i -g vercel
vercel login
vercel --prod            # from the repository root
```

No environment variables are needed on Vercel, and none should be added. The
Anthropic key and the Cube secret live only on the backend host. Nothing in
`web/` reads a secret, and the frontend never talks to Anthropic or to Cube.

**The risk worth knowing.** Server-sent events through a platform proxy can be
buffered, and if Vercel buffers the stream the investigation trace arrives in one
lump after the answer is already finished, instead of building live. Test it,
and if the trace does not build progressively, use the backend URL directly.

If you would rather have the frontend call the backend directly instead of
proxying, set `window.API_BASE` in `web/index.html` and put the frontend origin in
`CORS_ORIGINS` on the server. The code supports it: the cookie switches to
`SameSite=None` and CORS allows credentials. It is the arrangement browsers are
least happy with, so the proxy is the better default.

---

## When something fails

| What broke | Fall back to |
|---|---|
| Live model call fails or hangs | the mode toggle: replay is a recording of the same loop, and the app says so on screen |
| Anthropic key missing or expired | nothing to do, the app already falls back to replay and shows a notice |
| Cube down | replay still works; live questions get a clear "semantic layer unavailable" rather than a wrong answer |
| The whole UI | the API answers directly: `curl -b ck.txt $B/api/dashboard`, plus `ONE_PAGER.pdf` |

---

## Housekeeping

```bash
docker compose logs -f api            # application
docker compose logs -f caddy          # certificates and requests
docker compose restart api            # after an env change
docker system prune -f                # 20 GB fills up faster than you expect
```

When you are done with it, terminate the instance **and** release the Elastic IP.
An unattached Elastic IP is charged by the hour, which is the classic way to
leave a small bill running after a demo is over.
