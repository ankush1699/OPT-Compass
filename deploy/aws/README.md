# AWS deployment (AWS CLI scripts)

Status: **deployed and tested on 2026-11-02** (first run of `up.sh` worked without changes). Live URL while it is up:
`https://d3rqglo679t8bn.cloudfront.net/` (it is deleted with `./down.sh`, so it may not be running when you read this).
The monthly cost below is still an estimate; no AWS bill exists yet (see "Not done / not verified").

## What it creates (us-east-1, all tagged `Project=opt-compass`)

| Resource | Why | Rough monthly cost |
|---|---|---|
| EC2 `t4g.small` (2 GB RAM, ARM), Ubuntu 24.04, 20 GB gp3 disk | runs `docker compose` (the database and the service). The service measured about 1.3 GB of memory with both models loaded; a 2 GB swap file is added as a safety net | about $12.26 server + $1.60 disk |
| Elastic IP (a fixed public IPv4 address) | keeps the CloudFront origin address stable | about $3.65 |
| Security group | allows port 80 **only from CloudFront** (AWS's managed prefix list); nothing else is open, not even SSH | $0 |
| CloudFront distribution | gives a free `https://dxxxx.cloudfront.net` address with a certificate, so no domain is needed; no caching, all methods and headers passed through | $0 inside its free tier |

About **$17.50 a month** in total while it runs. The server and IP prices come from third-party price pages, the disk and
CloudFront numbers from memory; none was checked against AWS's own pricing pages yet. Compare with the real bill after the first
days (Billing and Cost Management, Cost Explorer) and write the measured figure here.

## Commands

```bash
cd deploy/aws
./up.sh --check   # look everything up and ask EC2 for a dry-run launch; creates nothing
./up.sh           # create it; prints a summary and waits for you to type "yes"
./status.sh       # what exists, the health check, how to read /stats; --log shows the server's console output
./down.sh         # delete everything (asks for "yes"), then lists what is left; safe to run twice
```

`up.sh` reads `LLM_MODEL` and `GEMINI_API_KEY` from the repo-root `.env` and needs the branch (`main` by default, or
`GIT_REF=<branch>`) to be on GitHub, because the server clones it and builds the image there (about 10 minutes the first
time). It generates the API key, stats key, analytics salt and database password itself; the two keys you need are in
`deploy/aws/.state/secrets.env` (gitignored, mode 600) and are never printed.

## How the pieces connect

```
visitor --https--> CloudFront --http, port 80--> EC2 (Elastic IP) --> docker compose: api (port 8000) --> db (Postgres + pgvector)
                   (free *.cloudfront.net)       security group: CloudFront only
```

The service runs with `DEMO_ENABLED=1` (public chat page), `TRUST_PROXY_HEADERS=1` (the per-visitor limits use the client
address CloudFront adds to `X-Forwarded-For`) and `API_KEYS=owner:<generated>`. Because the security group admits only
CloudFront, a visitor cannot reach the server directly and cannot forge that header.

## Security notes and trade-offs

- **The Gemini key goes into the instance's user data** (the first-boot script), which anyone with EC2 read access in this
  AWS account can see, and which stays there until the instance is deleted. The server also keeps it in `/opt/opt-compass/.env`
  (mode 600). It is a free-tier key, and `down.sh` deletes the instance. The alternative, SSM Parameter Store with an instance
  role, needs more IAM and was left out to keep the deployment small.
- The instance requires IMDSv2 with a hop limit of 1, so a process inside a container cannot read instance metadata.
- No SSH. To debug, use `./status.sh --log` (EC2 console output) or open a Session Manager or SSH path deliberately.
- `iam-policy.json` is a narrower policy for the deploy user than `AmazonEC2FullAccess` plus `CloudFrontFullAccess`. **It has
  not been tested yet**: run `./up.sh --check` and a full `up.sh` / `down.sh` cycle with it attached before trusting it.

## Cost control

- A budget alert is set in the AWS console (this repo does not manage it).
- `./down.sh` stops all billing for these resources. Stopping the server instead keeps charging for the disk and the IP.
- The in-memory demo limits (150 questions a day, 4 a minute per visitor) cap use of the free Gemini quota, not AWS cost.

## What the first real run showed (2026-11-02, us-east-1, `main` at 1722ea6)

All from commands run that day against the live URL:
- `up.sh` finished with the health check passing (`{"status":"ok","chunks":550}`); the corpus snapshot restored on the server. It
  needed about 6 to 9 minutes from start to a passing health check (the script's own wait loop printed "still waiting (6 min)" once).
- The page (`/`) returns 200 with the strict Content-Security-Policy, `nosniff` and `no-referrer` headers; `http://` redirects to
  `https://`; `/ask` without a key returns 401.
- The server's Elastic IP does not answer directly (curl on port 80 timed out), so only CloudFront can reach it.
- A question through `/demo/ask` was answered with cited sources. Time to the first event was 4.2 to 4.3 s on three warm requests
  (6.5 s on the first, cold request); the answer finished 5.1 to 6.8 s after sending (7.4 s on the cold one). These are four
  requests from one Mac, not a load test.
- Streaming: the answer arrived as separate events spread over a few hundred milliseconds (for example 7.1 s, 7.2 s, 7.3 s, 7.4 s),
  so CloudFront did not hold the whole reply until the end. The model sends only 2 to 5 text chunks per answer, so this is not a
  fine-grained token-by-token test.
- An off-topic question ("What is the capital of France?") ended as an abstention. `/stats` with the stats key worked.
- `HEAD /` returns 405 (the page only answers GET), which matters only for monitors that use HEAD.
- The analytics table now holds these test events (4 questions, 2 bot views), because there is no way to clear it without
  server access.

## Not done / not verified
- **No real bill yet.** Cost Explorer shows spend a day or so later; the figure in this file is an estimate until then.

- `down.sh` has not been run against a real deployment yet, and `iam-policy.json` has not been tried.
- No load test, no test from a phone, and the server's memory use was measured only locally (about 1.3 GB), not on the server.
- No HTTPS to the origin (CloudFront to the server is plain HTTP on AWS's network path). Fine for this project, but it is a
  choice, not a default best practice.
- No domain name, no WAF, no CloudWatch alarms, no backups of the database (it can be rebuilt from `snapshot/`; the analytics
  events cannot).
