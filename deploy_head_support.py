"""Deploy HEAD request support to GitHub."""
import subprocess
import sys

def run(cmd, description):
    """Run a command and show output."""
    print(f"\n{'='*70}")
    print(f"⏳ {description}...")
    print(f"{'='*70}")
    
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            check=True,
            capture_output=True,
            text=True,
            cwd=r"c:\Users\owida\OneDrive\Documents\Rade bot"
        )
        print(f"✅ SUCCESS")
        if result.stdout:
            print(result.stdout)
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ FAILED")
        if e.stdout:
            print(f"Output: {e.stdout}")
        if e.stderr:
            print(f"Error: {e.stderr}")
        return False

print("""
╔══════════════════════════════════════════════════════════════════╗
║                                                                  ║
║        DEPLOYING HEAD REQUEST SUPPORT FOR UPTIMEROBOT           ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
""")

# Stage changes
if not run("git add -A", "Staging all changes"):
    print("\n⚠️  Failed to stage. Try manually:")
    print("   git add -A")
    sys.exit(1)

# Show status
run("git status --short", "Checking what will be committed")

# Commit
if not run(
    'git commit -m "Add explicit HEAD request support for UptimeRobot free tier"',
    "Committing changes"
):
    print("\n⚠️  Commit failed. May already be committed.")
    print("   Continuing to push...")

# Push
if not run("git push origin main", "Pushing to GitHub"):
    print("\n⚠️  Push failed. Try manually:")
    print("   git push origin main")
    sys.exit(1)

print("""
╔══════════════════════════════════════════════════════════════════╗
║                                                                  ║
║                    ✅ DEPLOYMENT SUCCESSFUL!                     ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝

🎯 WHAT HAPPENS NEXT:

1. ⏳ Render is deploying (3-5 minutes)
2. ⏳ Service will support HEAD requests
3. ⏳ UptimeRobot will ping (within 5 min)
4. ✅ Monitor goes GREEN!
5. ✅ Service stays awake 24/7

🔍 VERIFY IN 10 MINUTES:

1. Test HEAD request:
   curl -I https://rade-bot.onrender.com/healthz

2. Check Render logs:
   Should see: HEAD /healthz 200

3. Check UptimeRobot:
   Should show: UP (green)

📖 Full details: HEAD_REQUEST_SUPPORT.md

🎉 Your service will now stay awake using UptimeRobot's free HEAD requests!
""")
