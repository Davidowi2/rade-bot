@echo off
echo ============================================================================
echo        DEPLOYING HEAD REQUEST SUPPORT FOR UPTIMEROBOT FREE TIER
echo ============================================================================
echo.

cd /d "c:\Users\owida\OneDrive\Documents\Rade bot"

echo Step 1: Staging changes...
git add -A
if %errorlevel% neq 0 (
    echo ERROR: Failed to stage changes
    pause
    exit /b 1
)
echo [OK] Changes staged
echo.

echo Step 2: Checking what will be committed...
git status --short
echo.

echo Step 3: Committing...
git commit -m "Add explicit HEAD request support for UptimeRobot free tier"
if %errorlevel% neq 0 (
    echo NOTE: Commit may have failed if nothing changed
    echo Continuing to push anyway...
)
echo.

echo Step 4: Pushing to GitHub...
git push origin main
if %errorlevel% neq 0 (
    echo ERROR: Push failed
    pause
    exit /b 1
)
echo [OK] Pushed to main
echo.

echo ============================================================================
echo                        DEPLOYMENT SUCCESSFUL!
echo ============================================================================
echo.
echo What happens next:
echo   1. Render deploys in 3-5 minutes
echo   2. Service will support HEAD requests
echo   3. UptimeRobot pings (HEAD method)
echo   4. Monitor goes GREEN
echo   5. Service stays awake 24/7
echo.
echo Verify in 10 minutes:
echo   1. curl -I https://rade-bot.onrender.com/healthz
echo   2. Check Render logs for: HEAD /healthz 200
echo   3. Check UptimeRobot shows: UP
echo.
echo ============================================================================
pause
