"""Comprehensive health check for Rade Bot."""
import requests
import json
from datetime import datetime

print("=" * 80)
print("🔍 RADE BOT - COMPREHENSIVE HEALTH CHECK")
print("=" * 80)
print(f"Timestamp: {datetime.utcnow().isoformat()}Z\n")

def check_endpoint(name, url, timeout=30):
    """Check an endpoint and report results."""
    print(f"\n{'─' * 80}")
    print(f"📍 {name}")
    print(f"   URL: {url}")
    print(f"{'─' * 80}")
    
    try:
        response = requests.get(url, timeout=timeout)
        print(f"✅ Status Code: {response.status_code}")
        
        if response.status_code == 200:
            try:
                data = response.json()
                print(f"✅ Response is valid JSON")
                print(f"\n📊 Response Data:")
                print(json.dumps(data, indent=2))
                return True, data
            except:
                print(f"⚠️  Response is not JSON:")
                print(response.text[:500])
                return False, None
        else:
            print(f"❌ Non-200 status code")
            print(f"Response: {response.text[:500]}")
            return False, None
            
    except requests.exceptions.Timeout:
        print(f"❌ TIMEOUT - Service not responding (might be cold starting)")
        print(f"   This is normal if service just deployed. Wait 60s and retry.")
        return False, None
    except requests.exceptions.ConnectionError as e:
        print(f"❌ CONNECTION ERROR: {e}")
        print(f"   Service might be down or deploying.")
        return False, None
    except Exception as e:
        print(f"❌ ERROR: {type(e).__name__}: {e}")
        return False, None

# 1. Health Check
print("\n" + "=" * 80)
print("1️⃣  HEALTH ENDPOINT CHECK")
print("=" * 80)
health_ok, health_data = check_endpoint(
    "Health Check", 
    "https://rade-bot.onrender.com/healthz",
    timeout=60
)

# 2. Status API
print("\n" + "=" * 80)
print("2️⃣  STATUS API CHECK")
print("=" * 80)
status_ok, status_data = check_endpoint(
    "Status API",
    "https://rade-bot.onrender.com/api/status",
    timeout=60
)

# Analysis
print("\n" + "=" * 80)
print("📊 ANALYSIS & RECOMMENDATIONS")
print("=" * 80)

if health_ok and status_ok:
    print("\n✅ SERVICE IS HEALTHY")
    
    # Check specific values
    if status_data:
        print("\n🔍 Checking Configuration:")
        
        max_stop = status_data.get('max_stop_distance')
        if max_stop:
            if max_stop >= 15.0:
                print(f"  ✅ max_stop_distance: {max_stop} (expected 15.0)")
            else:
                print(f"  ⚠️  max_stop_distance: {max_stop} (expected 15.0)")
        
        sizing = status_data.get('sizing', {})
        risk_usd = sizing.get('max_trade_risk_usd')
        if risk_usd:
            if risk_usd >= 30:
                print(f"  ✅ max_trade_risk_usd: {risk_usd} (expected 30)")
            else:
                print(f"  ⚠️  max_trade_risk_usd: {risk_usd} (expected 30)")
        
        guards = status_data.get('guards', {})
        slippage = guards.get('max_slippage')
        if slippage:
            if slippage >= 3.0:
                print(f"  ✅ max_slippage: {slippage} (expected 3.0)")
            else:
                print(f"  ⚠️  max_slippage: {slippage} (expected 3.0)")
        
        intake_ready = status_data.get('intake_ready')
        print(f"  {'✅' if intake_ready else '❌'} intake_ready: {intake_ready}")
        
        listener = status_data.get('listener', {})
        listener_healthy = listener.get('healthy')
        print(f"  {'✅' if listener_healthy else '❌'} listener.healthy: {listener_healthy}")
        
        executor = status_data.get('executor', {})
        executor_enabled = executor.get('enabled')
        print(f"  {'✅' if executor_enabled else '⚠️ '} executor.enabled: {executor_enabled}")

elif not health_ok:
    print("\n❌ HEALTH ENDPOINT FAILED")
    print("\n🔧 Possible Causes:")
    print("  1. Service is still deploying (wait 3-5 minutes)")
    print("  2. Service is cold starting (wait 60 seconds)")
    print("  3. Service crashed (check Render logs)")
    print("  4. Configuration error (check environment variables)")
    
    print("\n📋 Next Steps:")
    print("  1. Wait 5 minutes and run this script again")
    print("  2. Check Render dashboard for deployment status")
    print("  3. Check Render logs for errors")

elif not status_ok:
    print("\n⚠️  HEALTH OK BUT STATUS API FAILED")
    print("\n🔧 Possible Causes:")
    print("  1. API requires authentication token")
    print("  2. Service partially started")
    
    print("\n📋 Next Steps:")
    print("  1. Service is responding, likely okay")
    print("  2. Check if MIRROR_API_TOKEN is set correctly")

print("\n" + "=" * 80)
print("3️⃣  UPTIMEROBOT STATUS")
print("=" * 80)
print("\nUptimeRobot Configuration:")
print("  URL: https://rade-bot.onrender.com/healthz")
print("  Timeout: 30 seconds")
print("  Interval: 5 minutes")
print("  Expected: 200 OK")
print("\nIf UptimeRobot shows DOWN:")
print("  - First ping after deploy takes 30-60s (cold start)")
print("  - Check timeout is set to 30s (not 10-15s)")
print("  - Verify URL includes /healthz")
print("  - Wait for next ping cycle (up to 5 min)")

print("\n" + "=" * 80)
print("✅ HEALTH CHECK COMPLETE")
print("=" * 80)
print(f"\nRun this again in 5 minutes if service is still deploying.")
print("Check Render logs at: https://dashboard.render.com\n")
