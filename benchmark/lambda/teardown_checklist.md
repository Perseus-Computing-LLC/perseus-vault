# Teardown checklist — DO THIS or you leak credits

Lambda bills weekly and the balance does NOT move mid-cycle. An idle A100 at
$1.99/hr = ~$334/week that you will NOT see decrement until the cycle closes —
and any overage past the $7,500 credit hits the card on file. So: **when you are
not actively using the box, terminate it.** The Persistent Filesystem keeps your
data (~$0.20/GB/mo), so teardown is cheap and fully reversible.

## Before you terminate

- [ ] **Copy Gauntlet outputs off the ephemeral instance disk to the PFS:**
      ```bash
      cp ~/perseus-repo/benchmark/gauntlet/v2/gauntlet_v2_report.md   $PFS/logs/
      cp ~/perseus-repo/benchmark/gauntlet/v2/gauntlet_v2_results.json $PFS/logs/
      cp ~/perseus-repo/benchmark/gauntlet/v2/gauntlet_v2_score.txt    $PFS/logs/
      ```
- [ ] **Pull the report to a durable place** (your laptop / the repo) so it's
      citable in submissions, the website, and grant apps:
      ```bash
      # from your local machine:
      scp ubuntu@<instance-ip>:$PFS/logs/gauntlet_v2_report.md ./
      ```
- [ ] Confirm models are on the PFS (they are, via OLLAMA_MODELS override) so the
      next launch skips the multi-GB re-download.
- [ ] Note the score + date to append to the perseus-gauntlet-v2 skill's
      "Baseline Scores" table (this run = FIRST real-inference number).

## Terminate

- [ ] **Lambda console → Instances → Terminate.** (CLI: `lambda instances terminate <id>` if you set up the CLI.)
- [ ] **Do NOT delete the Persistent Filesystem** — that's what makes relaunch cheap.
- [ ] Confirm in the console that the instance shows Terminated (stops the meter).

## Relaunch later (fast path)

1. Launch a new A100 80GB, attach the SAME Persistent Filesystem.
2. `cd lambda-kit && ./serve.sh`  (models already cached — no re-download)
3. Endpoint live in ~1 min. Run benchmarks or serve as needed.

## Weekly credit sanity check

- [ ] Once per weekly cycle, note the "Remaining service credit" figure and
      subtract from last week's to see real burn. If burn > plan, something is
      idling — terminate it.
