# AWS deployment (AWS CLI scripts)

Status: **scripts written and checked with read-only calls and an EC2 dry run; not yet run for real.** Nothing here has
created a resource. This file is updated with the live URL, the measured cost and the problems found after the first run.

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
- CloudFront does not buffer by itself, but whether the server-sent-event stream arrives token by token through it has to be
  checked on the live site; it is not claimed here until it has been.

## Cost control

- A budget alert is set in the AWS console (this repo does not manage it).
- `./down.sh` stops all billing for these resources. Stopping the server instead keeps charging for the disk and the IP.
- The in-memory demo limits (150 questions a day, 4 a minute per visitor) cap use of the free Gemini quota, not AWS cost.

## Not done / not verified

- No real run yet: the first `up.sh` may fail on a detail only a real launch shows (for example the security-group rule
  count for the CloudFront prefix list, or free-plan instance limits; only the EC2 dry run said the launch is allowed).
- No HTTPS to the origin (CloudFront to the server is plain HTTP on AWS's network path). Fine for this project, but it is a
  choice, not a default best practice.
- No domain name, no WAF, no CloudWatch alarms, no backups of the database (it can be rebuilt from `snapshot/`; the analytics
  events cannot).
