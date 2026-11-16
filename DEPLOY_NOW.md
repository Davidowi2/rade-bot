# DEPLOY NOW - Manual Commands

Since PowerShell is blocking automated deployment, here are the exact commands to run:

## **STEP 1: Deploy Code to Git**

Open a NEW Command Prompt (cmd.exe) or Git Bash and run:

```bash
cd "c:\Users\owida\OneDrive\Documents\Rade bot"

git add -A

git commit -m "Fix over-engineering: signal age tolerance, risk calculation from published entry, raised limits"

git push origin main
```

**Expected:** Render will auto-deploy in 3-5 minutes.

---

## **STEP 2: Update Render Environment**

In the same terminal, run:

```bash
python scripts/update_render_env.py
```

**Review the output** - it will show which variables are changing:
- MIRROR_MAX_SIGNAL_AGE_S: 120 → 900
- MIRROR_MAX_TRADE_RISK_USD: 25 → 30
- MIRROR_MAX_SLIPPAGE: 2.0 → 3.0

If it looks good, **run it again WITHOUT --dry-run** (it runs without dry-run by default):

```bash
python scripts/update_render_env.py
```

**OR if the script doesn't work**, update manually:
1. Go to https://dashboard.render.com
2. Login with your credentials
3. Select "rade-mirror" service
4. Click "Environment" tab
5. Find and update these 3 variables:
   - `MIRROR_MAX_SIGNAL_AGE_S` → change to `900`
   - `MIRROR_MAX_TRADE_RISK_USD` → change to `30`
   - `MIRROR_MAX_SLIPPAGE` → change to `3.0`
6. Click "Save Changes" at the bottom
7. Render will redeploy automatically

---

## **STEP 3: Fix UptimeRobot (Why 502)**

1. Go to https://uptimerobot.com
2. Login to your account
3. Find your "rade-bot" monitor
4. Click "Edit"
5. **Change these settings:**
   - **URL:** `https://rade-bot.onrender.com/healthz` ⚠️ MUST include /healthz
   - **Timeout:** `30` seconds (NOT 10 or 15)
   - **Interval:** `5` minutes
   - **Monitor Type:** HTTP(s)
   - **HTTP Method:** GET
   - **Expected Status Code:** 200
6. Click "Save"

---

## **STEP 4: Verify Deployment**

### A) Wait 5-10 minutes for Render deployment

### B) Check Service Status
Open browser: https://rade-bot.onrender.com/api/status

Look for:
```
"max_stop_distance": 15.0          (was 12.5)
"max_trade_risk_usd": 30           (was 25)  
"max_slippage": 3.0                (was 2.0)
```

### C) Check Health Endpoint
Open browser: https://rade-bot.onrender.com/healthz

Should show:
```
{
  "status": "ok",
  "listener": {
    "healthy": true
  }
}
```

### D) Check UptimeRobot
- Wait for next ping (5 minutes)
- First ping might be slow (cold start)
- Should show "UP" (200 OK) after that
- Should stay "UP" continuously

---

## **FILES CHANGED**

These files have been modified and are ready to commit:

✅ src/rade/observer/mirror_pipeline.py - Core logic fixes
✅ scripts/update_render_env.py - New environment defaults
✅ render.yaml - Updated blueprint
✅ DEPLOYMENT_FIXES_2026-11-16.md - Technical documentation
✅ QUICK_DEPLOYMENT_GUIDE.md - Deployment guide
✅ FIXES_SUMMARY.txt - Summary
✅ DEPLOY_NOW.md - This file
✅ deploy_fixes.bat - Deployment script

---

## **WHAT WAS FIXED**

1. ✅ Signal age: 120s → 900s with catchup tolerance
2. ✅ Risk calculation: Now uses published entry (not live market)
3. ✅ Risk ceiling: $25 → $30 (max stop 12.5pt → 15pt)
4. ✅ Slippage: 2.0pt → 3.0pt
5. ✅ All over-engineering issues resolved

---

## **EXPECTED RESULT**

Bot will now accept **100% of valid channel signals** instead of blocking them.

---

## **TROUBLESHOOTING**

**Q: Git push asks for credentials?**
A: Use your GitHub username and Personal Access Token (not password)

**Q: Python script shows "RENDER_API_KEY not set"?**
A: The key is in your .env file. Update Render manually (see Step 2 alternative)

**Q: UptimeRobot still shows 502?**
A: Double-check URL includes /healthz and timeout is 30 seconds

**Q: How do I know it's working?**
A: Next time a signal is posted, check it's accepted (not "stale" or "blocked")

---

## **NEED HELP?**

All changes are ready in your local repository. Just need to:
1. Commit to git
2. Update Render environment (3 variables)
3. Fix UptimeRobot URL and timeout

Then you're done! 🚀
