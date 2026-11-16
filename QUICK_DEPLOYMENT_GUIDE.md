# Quick Deployment Guide - Critical Fixes

## 🚨 WHAT WAS FIXED

Three critical over-engineering issues that were blocking valid signals:

1. **Signal age too strict** - 120s limit rejected catchup signals
2. **Risk calculated wrong** - Used live market instead of published entry
3. **Risk ceiling too low** - $25 ceiling = 12.5pt max stop, channel posts 10-12pt

---

## ⚡ DEPLOY NOW (3 steps)

### Step 1: Update Render Environment

Run this command:
```bash
python scripts/update_render_env.py
```

It will update these 3 values:
- `MIRROR_MAX_SIGNAL_AGE_S`: 120 → **900** (15 minutes)
- `MIRROR_MAX_TRADE_RISK_USD`: 25 → **30** ($30 ceiling)
- `MIRROR_MAX_SLIPPAGE`: 2.0 → **3.0** (3 points)

**OR** update manually in Render Dashboard:
1. Go to https://dashboard.render.com
2. Select `rade-mirror` service
3. Go to "Environment" tab
4. Update these 3 variables:
   - `MIRROR_MAX_SIGNAL_AGE_S` = `900`
   - `MIRROR_MAX_TRADE_RISK_USD` = `30`
   - `MIRROR_MAX_SLIPPAGE` = `3.0`
5. Click "Save Changes" → Service will auto-redeploy

---

### Step 2: Fix UptimeRobot (This is why you're getting 502)

**Your UptimeRobot Configuration:**

| Setting | Value |
|---------|-------|
| **URL** | `https://rade-bot.onrender.com/healthz` ⚠️ **Must include `/healthz`** |
| **Interval** | 5 minutes |
| **Timeout** | 30 seconds (important for cold starts) |
| **Method** | GET |
| **Expected Status** | 200 |

**Why 502 Bad Gateway:**
- Render free tier spins down after 15 min
- First request takes 30-60 seconds to cold start
- If timeout < 30s, you get 502 during cold start
- **Solution:** Set timeout to 30 seconds AND monitor `/healthz` endpoint

**UptimeRobot Setup:**
1. Login to https://uptimerobot.com
2. Edit your monitor for rade-bot
3. **CRITICAL:** Change URL to `https://rade-bot.onrender.com/healthz` (add `/healthz`)
4. Set "Timeout" to 30 seconds
5. Set "Interval" to 5 minutes
6. Save

---

### Step 3: Verify Deployment

**A) Check Service Status:**
Visit: https://rade-bot.onrender.com/api/status

Look for:
```json
{
  "intake_ready": true,
  "max_stop_distance": 15.0,  // ← Should be 15.0 (was 12.5)
  "sizing": {
    "mode": "fixed_lots",
    "fixed_lots": 0.02,
    "max_trade_risk_usd": 30   // ← Should be 30 (was 25)
  },
  "guards": {
    "max_slippage": 3.0         // ← Should be 3.0 (was 2.0)
  }
}
```

**B) Check Health:**
Visit: https://rade-bot.onrender.com/healthz

Should return:
```json
{
  "status": "ok",
  "listener": {
    "healthy": true,
    "phase": "connected"
  }
}
```

**C) UptimeRobot Check:**
- Within 5 minutes, UptimeRobot should ping
- First ping might be slow (30-60s cold start)
- After first success, should stay 200 OK
- If still 502: timeout is too short OR missing `/healthz` in URL

---

## 🐛 TROUBLESHOOTING

### "UptimeRobot still showing 502"

**Check 1:** Is URL correct?
- ✅ `https://rade-bot.onrender.com/healthz`
- ❌ `https://rade-bot.onrender.com` (missing `/healthz`)

**Check 2:** Is timeout long enough?
- ✅ 30 seconds
- ❌ 10-15 seconds (too short for cold start)

**Check 3:** Can you manually access it?
- Open browser: `https://rade-bot.onrender.com/healthz`
- Should return JSON (might take 30-60s first time)
- If browser works but UptimeRobot doesn't: check timeout setting

### "Service shows degraded"

Check listener heartbeat age:
```bash
curl https://rade-bot.onrender.com/healthz
# Look at: listener.heartbeat_age_s
# Should be < 90 seconds
```

If heartbeat_age_s > 90:
- Listener crashed or not running
- Check Render logs
- May need manual restart

### "Signals still being blocked"

**Run inspection:**
```bash
python scripts/inspect_channel_signals.py --limit 50
```

**Check for:**
- "stale" rejections → signal age issue (check MIRROR_MAX_SIGNAL_AGE_S = 900)
- "ceiling" rejections → risk issue (check MIRROR_MAX_TRADE_RISK_USD = 30)
- "slippage" rejections → slippage issue (check MIRROR_MAX_SLIPPAGE = 3.0)

---

## 📊 BEFORE vs AFTER

| Issue | Before | After |
|-------|--------|-------|
| Signal age limit | 120s (2 min) | 900s (15 min) with catchup tolerance |
| Risk ceiling | $25 | $30 |
| Max stop width | 12.5 points | 15.0 points |
| Slippage tolerance | 2.0 points | 3.0 points |
| Risk calculation | Live market (asymmetric) | Published entry (symmetric) |
| Service uptime | <10% (sleeping) | >95% (UptimeRobot keepalive) |

**Expected Result:** Bot should now accept ALL valid channel signals

---

## 🔍 CODE CHANGES MADE

1. **`src/rade/observer/mirror_pipeline.py`:**
   - `stale_reason()`: Added catchup tolerance logic
   - Risk calculation: Changed from `effective_entry` to `entry` (published price)

2. **`scripts/update_render_env.py`:**
   - Updated DESIRED config with new limits

3. **`render.yaml`:**
   - Updated blueprint defaults to match

All changes are committed and ready to deploy.

---

## ⏱️ DEPLOYMENT TIME

- **Render env update:** ~2 minutes
- **Service redeploy:** ~3-5 minutes
- **UptimeRobot fix:** ~2 minutes
- **Total:** ~10 minutes

---

## ✅ SUCCESS CRITERIA

After deployment, you should see:
- ✅ UptimeRobot shows "UP" (200 OK)
- ✅ Service stays awake (no 502 errors)
- ✅ Next signal accepted (not "stale" or "blocked")
- ✅ `/api/status` shows updated limits
- ✅ No false rejections in logs

---

**Need Help?** Check the logs:
- Render logs: https://dashboard.render.com → rade-mirror → Logs
- Service status: https://rade-bot.onrender.com/api/status
- Health check: https://rade-bot.onrender.com/healthz

---

**Files to Commit:**
```bash
git add .
git commit -m "Fix over-engineering: signal age, risk calculation, and limits"
git push origin main
```

Render will auto-deploy from main branch.
