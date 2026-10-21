# Manual deploy checklist (backend + worker)

Production = the `production` branch. Render services **"Ai Vastra Backend 2.3"** and
**"Ai Vastra Worker 2.3"** deploy from it. Auto-Deploy is OFF: nothing goes live until you
do the steps below. All commands are PowerShell, run in the repo folder.

## 1. Before you start
- [ ] Quiet time: late night, or before shops open.
- [ ] Check for pending SQL files (on `main` but not yet on `production`):
  ```powershell
  git fetch origin
  git diff --name-only origin/production origin/main -- sql/
  ```
- [ ] If that lists any files:
  - [ ] Supabase → Database → Backups: confirm today's daily backup exists (Pro plan).
  - [ ] Run each SQL file in the Supabase SQL editor, **in filename (date) order**.
- [ ] New environment variables (named in the task reports): add them in Render to **both**
      "Ai Vastra Backend 2.3" **and** "Ai Vastra Worker 2.3" first.

## 2. Merge main into production
```powershell
git checkout main
git pull
git status            # must be clean of changes you care about

git checkout production
git pull

$d = Get-Date -Format yyyy-MM-dd
git branch "backup-prod-$d"
git push origin "backup-prod-$d"

git merge main
git push

git checkout main
```
If `git merge main` reports a conflict, stop: run `git merge --abort`, then
`git checkout main`, and ask for help. Nothing has been deployed at that point.

## 3. Deploy on Render
- [ ] "Ai Vastra Backend 2.3" → **Manual Deploy → Deploy latest commit**.
- [ ] "Ai Vastra Worker 2.3" → **Manual Deploy → Deploy latest commit**.
- [ ] Wait until **both** show **Live**.

## 4. Frontend
- [ ] If the change has a matching frontend part, deploy it now (see the frontend repo's `DEPLOY.md`).

## 5. Check it works
- [ ] Open `https://<backend-url>/healthz` → must show `{"ok": true}`.
      If it lists `missing` names, add those variables in Render and redeploy.
- [ ] Open the app and generate one look in the **demo shop**.
- [ ] Check the TV screen loads.
- [ ] Send `menu` on WhatsApp from **your own** number and check the reply.

## 6. If something is broken
- [ ] Render → each service → **Events** → find the previous successful deploy → **Rollback**.
      Do this on **both** "Ai Vastra Backend 2.3" and "Ai Vastra Worker 2.3".
- [ ] Note which commit was rolled back to and report it (also the date of the `backup-prod-...` branch).
- [ ] A rollback does not change the `production` branch, so it still contains the broken
      commit. Don't deploy again until the fix is merged from `main`.

## One-time: Render health check
After the first deploy that contains `/healthz`, once step 5 shows `{"ok": true}`:
Render → "Ai Vastra Backend 2.3" → Settings → **Health Check Path** = `/healthz`.
This applies to the backend web service only. A Background Worker service has no health
check setting.
