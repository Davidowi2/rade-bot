@echo off
echo ================================================================================
echo DEPLOYING CRITICAL FIXES TO RADE BOT
echo ================================================================================
echo.

echo Step 1: Staging all changes...
git add -A
if %errorlevel% neq 0 (
    echo ERROR: Failed to stage changes
    pause
    exit /b 1
)
echo ✓ Changes staged
echo.

echo Step 2: Committing changes...
git commit -m "Fix over-engineering: signal age tolerance, risk calculation from published entry, raised limits"
if %errorlevel% neq 0 (
    echo ERROR: Failed to commit changes
    pause
    exit /b 1
)
echo ✓ Changes committed
echo.

echo Step 3: Pushing to remote...
git push origin main
if %errorlevel% neq 0 (
    echo ERROR: Failed to push changes
    pause
    exit /b 1
)
echo ✓ Changes pushed to main branch
echo.

echo ================================================================================
echo DEPLOYMENT TO GIT: SUCCESS
echo ================================================================================
echo.
echo Render will auto-deploy from main branch in ~3-5 minutes.
echo.
echo NEXT STEPS:
echo 1. Wait for Render deployment to complete
echo 2. Update Render environment variables (see QUICK_DEPLOYMENT_GUIDE.md)
echo 3. Fix UptimeRobot URL and timeout (see QUICK_DEPLOYMENT_GUIDE.md)
echo.
echo Would you like to update Render environment now? (Y/N)
set /p UPDATE_ENV=
if /i "%UPDATE_ENV%"=="Y" (
    echo.
    echo Running environment update...
    python scripts\update_render_env.py
    echo.
    echo Check output above for any errors.
)
echo.
echo Deployment complete! Check QUICK_DEPLOYMENT_GUIDE.md for verification steps.
pause
