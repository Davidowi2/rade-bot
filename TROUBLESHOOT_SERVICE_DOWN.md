# 🚨 SERVICE DOWN - TROUBLESHOOTING GUIDE

**Problem:** Render service showing as DOWN for 5h 2m 18s  
**UptimeRobot:** https://rade-bot.onrender.com/healthz returning errors

---

## 🔍 IMMEDIATE DIAGNOSTICS

### Step 1: Check Render Dashboard

**Go to:** https://dashboard.render.com

1. **Click on `rade-mirror` service**

2. **Check the STATUS badge (top right):**
   - 🟢 **"Live"** → Service running (go to Step 2)
   - 🔴 **"Deploy failed"** → Deployment error (go to Step 3)
   - 🟡 **"Deploying"** → Still deploying (wait 5 more min)
   - ⚪ **"Suspended"** → Free tier suspended (restart needed)

3. **Check "Events" tab:**
   - Look for latest deployment
   - Should show "Deploy succeeded" or "Deploy failed"
   - Note the timestamp

4. **Check "Logs" tab:**
   - Look for errors (red text)
   - Look for "Application started" or similar
   - Check last few lines for current status

---

## 🔧 COMMON ISSUES & FIXES

### Issue #1: Service Shows "Deploy Failed"

**Symptoms:**
- Render status: Deploy failed
- Logs show build or startup errors

**Possible Causes:**

#### A) Missing Requirements File
```
Error: Could not find requirements-mirror.txt
```

**Fix:**
```bash
# Check if file exists
ls requirements-mirror.txt

# If missing, it should contain:
python-telegram-bot
supabase
requests
python-dotenv
pydantic
pydantic-settings
```

#### B) Python Module Import Error
```
Error: No module named 'rade'
```

**Fix:** Check `PYTHONPATH` is set to `src` in Render Environment

#### C) Syntax Error in Code
```
SyntaxError: invalid syntax
```

**Fix:** Check recent commits for syntax errors
```bash
# Revert if needed
git revert HEAD
git push origin main
```

---

### Issue #2: Service Shows "Live" But Not Responding

**Symptoms:**
- Render shows "Live" with green dot
- But /healthz times out or returns error
- Logs might show crashes/restarts

**Possible Causes:**

#### A) Service Crashing on Startup

**Check Logs For:**
```
Exception: ...
Error: ...
Traceback: ...
```

**Common Startup Errors:**
1. **Missing Environment Variables**
   ```
   KeyError: 'SUPABASE_URL'
   ```
   **Fix:** Check Render → Environment tab has all required vars

2. **Database Connection Failed**
   ```
   Error connecting to Supabase
   ```
   **Fix:** Verify SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY

3. **Telegram Auth Failed**
   ```
   Telegram authentication error
   ```
   **Fix:** Check TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION

#### B) Port Binding Issue

**Check Logs For:**
```
Address already in use
Port ... is already allocated
```

**Fix:** Render manages ports automatically, but if you see this:
- Service might be restarting too fast
- Wait 5 minutes for Render to clean up

#### C) Health Check Failing

**Check Logs For:**
```
Health check failed
```

Render expects `/healthz` to return 200 within 30 seconds.

**Fix:**
- Listener might not be starting
- Check MIRROR_RUN_LISTENER=1
- Check Telegram credentials valid

---

### Issue #3: Service Suspended (Free Tier)

**Symptoms:**
- Status shows "Suspended" or similar
- No logs in past few hours

**Fix:**
1. Click "Resume Service" or "Restart"
2. Wait 2-3 minutes
3. Service should come back online

**Note:** Free tier can be suspended if:
- Account payment method issue
- Exceeded free tier limits
- Account verification needed

---

### Issue #4: Environment Variables Missing

**Symptoms:**
- Logs show KeyError or "not set"
- Service crashes immediately after start

**Check These Required Variables:**

```
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
TELEGRAM_API_ID
TELEGRAM_API_HASH
TELEGRAM_SESSION
TELEGRAM_CHANNEL
TRADELOCKER_EMAIL
TRADELOCKER_PASSWORD
TRADELOCKER_SERVER
TRADELOCKER_ACCOUNT_ID
MIRROR_API_TOKEN
```

**Fix:**
1. Go to Render → Environment tab
2. Check all required vars are set
3. Click "Save Changes"
4. Service will redeploy

---

## 🎯 STEP-BY-STEP RESOLUTION

### Step 1: Access Render Dashboard

1. Go to: https://dashboard.render.com
2. Login
3. Click on `rade-mirror` service

### Step 2: Check Current Status

Look at the status badge and note it down:
- [ ] Live (green)
- [ ] Deploying (yellow)
- [ ] Deploy failed (red)
- [ ] Suspended (gray)

### Step 3: Check Logs

1. Click "Logs" tab
2. Scroll to bottom
3. Look for errors (red text)
4. **Copy the last 20-30 lines** - we need this!

### Step 4: Check Events

1. Click "Events" tab
2. Find latest deploy event
3. Check if it says "succeeded" or "failed"
4. Note the timestamp

### Step 5: Based on Status, Choose Action

#### If Status = "Live":
- Check logs for crashes/errors
- Verify environment variables
- Try manual restart (top right menu)

#### If Status = "Deploy Failed":
- Check logs for build errors
- Check requirements-mirror.txt exists
- Check PYTHONPATH=src is set
- May need to fix code and redeploy

#### If Status = "Suspended":
- Click "Resume Service"
- Wait 3 minutes
- Check status again

---

## 🔨 MANUAL FIXES TO TRY

### Fix #1: Manual Restart

**When to use:** Service is "Live" but not responding

**Steps:**
1. Render Dashboard → rade-mirror
2. Top right menu (⋯) → "Restart Service"
3. Wait 3-5 minutes
4. Check /healthz again

### Fix #2: Force Redeploy

**When to use:** Deployment might be stuck or old

**Steps:**
1. Render Dashboard → rade-mirror
2. Top right "Manual Deploy" → Deploy latest commit
3. Wait for deployment to complete (3-5 min)
4. Check logs for errors

### Fix #3: Check and Fix Environment

**When to use:** Service crashes on startup

**Steps:**
1. Render Dashboard → Environment tab
2. Verify these 3 were updated:
   - `MIRROR_MAX_SIGNAL_AGE_S` = `900`
   - `MIRROR_MAX_TRADE_RISK_USD` = `30`
   - `MIRROR_MAX_SLIPPAGE` = `3.0`
3. Check all other required vars are present
4. Save Changes (auto-redeploys)

### Fix #4: Rollback if Needed

**When to use:** Service was working before our changes

**Steps:**
```bash
# Go back to previous commit
git log --oneline -5

# Revert to commit before our changes (f81f33b)
git revert 1719bcc
git push origin main

# Or reset to previous commit
git reset --hard f81f33b
git push origin main --force
```

**Warning:** Only do this as last resort!

---

## 📋 DIAGNOSTIC CHECKLIST

Before asking for help, collect this info:

- [ ] Render service status (Live/Failed/Suspended)
- [ ] Last 30 lines of logs (copy from Render)
- [ ] Last deploy event status (succeeded/failed)
- [ ] Last deploy timestamp
- [ ] Error messages from logs
- [ ] Environment variables count (should be ~44)

---

## 🆘 EMERGENCY QUICK FIXES

### Quick Fix #1: Restart Everything

```bash
# In Render Dashboard:
1. Click rade-mirror service
2. Top menu ⋯ → "Restart Service"
3. Wait 5 minutes
4. Test /healthz
```

### Quick Fix #2: Force Clean Deploy

```bash
# In Render Dashboard:
1. Settings → Danger Zone
2. "Clear Build Cache & Deploy"
3. Wait 5-7 minutes
4. Test /healthz
```

### Quick Fix #3: Verify Git Pushed

```bash
# In your terminal:
git log --oneline -3
# Should show: 1719bcc Fix over-engineering...

git remote -v
# Should show: github.com/Davidowi2/rade-bot

# Check GitHub shows latest commit:
# https://github.com/Davidowi2/rade-bot
```

---

## 📞 WHAT TO REPORT

If still stuck, provide:

1. **Render Status:** (Live/Failed/Suspended)

2. **Last Deploy Event:**
   ```
   [Copy from Events tab]
   ```

3. **Recent Logs:**
   ```
   [Copy last 30 lines from Logs tab]
   ```

4. **Environment Check:**
   - Number of environment variables: ___
   - MIRROR_MAX_SIGNAL_AGE_S value: ___
   - MIRROR_MAX_TRADE_RISK_USD value: ___

5. **Error Messages:**
   ```
   [Any red error text from logs]
   ```

---

## 🎯 MOST LIKELY CAUSES

Based on symptoms (down for 5h):

1. **Deployment Failed** (80% likely)
   - Check Render Events tab
   - Check Logs for build errors
   - May need to fix code issue

2. **Service Crashing on Startup** (15% likely)
   - Check Logs for Python exceptions
   - Check environment variables
   - May need to restart

3. **Free Tier Suspended** (5% likely)
   - Check account status
   - Click "Resume Service"

---

## 🚀 RECOMMENDED ACTION NOW

**Do this RIGHT NOW:**

1. Open: https://dashboard.render.com
2. Click: `rade-mirror` service
3. Look at: Status badge (top right)
4. Click: "Logs" tab
5. **Screenshot or copy the last 50 lines**
6. **Send me the logs**

Then I can tell you exactly what's wrong and how to fix it!

---

**Without seeing the logs, I can't tell what specific error is causing the down status.**

The logs will show one of:
- ✅ "Application started" → Service is running (different issue)
- ❌ Python error/exception → Code issue
- ❌ Build failed → Dependency issue
- ❌ Nothing recent → Service suspended

**GET THE LOGS AND WE'LL FIX IT!** 🔧
