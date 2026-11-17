"""Deploy the fixes to git and Render."""
import subprocess
import sys

def run_command(cmd, description):
    """Run a command and report results."""
    print(f"\n{'='*70}")
    print(f"⏳ {description}...")
    print(f"{'='*70}")
    
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            check=True,
            capture_output=True,
            text=True
        )
        print(f"✅ SUCCESS")
        if result.stdout:
            print(result.stdout)
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ FAILED: {e}")
        if e.stdout:
            print(f"Output: {e.stdout}")
        if e.stderr:
            print(f"Error: {e.stderr}")
        return False

def main():
    print("""
    ╔══════════════════════════════════════════════════════════════════╗
    ║          DEPLOYING RADE BOT CRITICAL FIXES                       ║
    ╚══════════════════════════════════════════════════════════════════╝
    """)
    
    # Step 1: Git status
    print("\n📋 Checking git status...")
    subprocess.run("git status --short", shell=True)
    
    # Step 2: Stage changes
    if not run_command("git add -A", "Staging all changes"):
        print("\n⚠️  Failed to stage changes. Try manually:")
        print("   git add -A")
        return 1
    
    # Step 3: Commit
    commit_msg = "Fix over-engineering: signal age tolerance, risk calculation from published entry, raised limits"
    if not run_command(f'git commit -m "{commit_msg}"', "Committing changes"):
        print("\n⚠️  Commit failed. Possible reasons:")
        print("   - No changes to commit (already committed)")
        print("   - Git configuration issue")
        print("\n   Try manually:")
        print(f'   git commit -m "{commit_msg}"')
        # Continue anyway - might already be committed
    
    # Step 4: Push
    if not run_command("git push origin main", "Pushing to GitHub"):
        print("\n⚠️  Push failed. Try manually:")
        print("   git push origin main")
        return 1
    
    print("""
    ╔══════════════════════════════════════════════════════════════════╗
    ║          ✅ CODE DEPLOYMENT SUCCESSFUL                           ║
    ╚══════════════════════════════════════════════════════════════════╝
    
    Render will auto-deploy in 3-5 minutes.
    
    NEXT STEPS:
    
    1. ⏳ Wait for Render deployment (3-5 min)
    
    2. 🔧 Update Render Environment (choose one):
       
       Option A - Automatic:
         python scripts/update_render_env.py
       
       Option B - Manual:
         Go to https://dashboard.render.com
         → rade-mirror → Environment
         → Update these 3 variables:
           MIRROR_MAX_SIGNAL_AGE_S = 900
           MIRROR_MAX_TRADE_RISK_USD = 30
           MIRROR_MAX_SLIPPAGE = 3.0
         → Save Changes
    
    3. 🌐 Fix UptimeRobot (this is why 502!):
       
       Go to https://uptimerobot.com
       → Edit your rade-bot monitor
       → URL: https://rade-bot.onrender.com/healthz (add /healthz!)
       → Timeout: 30 seconds
       → Interval: 5 minutes
       → Save
    
    4. ✅ Verify (after 10 minutes):
       
       Visit: https://rade-bot.onrender.com/api/status
       Check: max_stop_distance = 15.0 (was 12.5)
              max_trade_risk_usd = 30 (was 25)
              max_slippage = 0 / DISABLED (the size is fitted to the real entry)
    
    📖 Full docs: QUICK_DEPLOYMENT_GUIDE.md
    """)
    
    # Ask if they want to update Render env now
    print("\n" + "="*70)
    print("Would you like to update Render environment now? (y/n): ", end="")
    response = input().strip().lower()
    
    if response in ('y', 'yes'):
        print("\n" + "="*70)
        print("⏳ Updating Render environment...")
        print("="*70)
        result = subprocess.run(
            "python scripts/update_render_env.py",
            shell=True
        )
        if result.returncode == 0:
            print("\n✅ Render environment updated successfully!")
        else:
            print("\n⚠️  Update failed. Update manually in Render dashboard.")
    
    print("\n🎉 Deployment complete! Check QUICK_DEPLOYMENT_GUIDE.md for next steps.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
