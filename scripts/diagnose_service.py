"""Diagnose why the Render service is down."""
import requests
import time
from datetime import datetime

print("=" * 80)
print("🔍 DIAGNOSING RENDER SERVICE")
print("=" * 80)
print(f"Timestamp: {datetime.utcnow().isoformat()}Z\n")

endpoints = [
    ("Health Check", "https://rade-bot.onrender.com/healthz", 60),
    ("Root URL", "https://rade-bot.onrender.com/", 60),
    ("Status API", "https://rade-bot.onrender.com/api/status", 60),
]

for name, url, timeout in endpoints:
    print(f"\n{'─' * 80}")
    print(f"Testing: {name}")
    print(f"URL: {url}")
    print(f"Timeout: {timeout}s")
    print(f"{'─' * 80}")
    
    try:
        start = time.time()
        response = requests.get(url, timeout=timeout)
        elapsed = time.time() - start
        
        print(f"✅ Response received in {elapsed:.2f}s")
        print(f"Status Code: {response.status_code}")
        print(f"Headers:")
        for key, value in response.headers.items():
            print(f"  {key}: {value}")
        
        print(f"\nResponse Body ({len(response.text)} bytes):")
        if len(response.text) < 2000:
            print(response.text)
        else:
            print(response.text[:2000] + "\n... (truncated)")
            
    except requests.exceptions.Timeout:
        print(f"❌ TIMEOUT after {timeout}s")
        print("   Service is not responding")
        print("   Possible causes:")
        print("   - Service is stopped/crashed")
        print("   - Service is still starting (cold start)")
        print("   - Service deployment failed")
        
    except requests.exceptions.ConnectionError as e:
        print(f"❌ CONNECTION ERROR")
        print(f"   {e}")
        print("   Possible causes:")
        print("   - Service is not running")
        print("   - Render deployment failed")
        print("   - Network/DNS issue")
        
    except Exception as e:
        print(f"❌ ERROR: {type(e).__name__}")
        print(f"   {e}")

print("\n" + "=" * 80)
print("💡 RECOMMENDATIONS")
print("=" * 80)

print("""
1. Check Render Dashboard:
   https://dashboard.render.com
   → Select 'rade-mirror' service
   → Check 'Events' tab for deployment status
   → Check 'Logs' tab for errors

2. Common Issues:
   - Deployment failed (check logs for Python errors)
   - Service crashed on startup (check logs)
   - Environment variables missing (check Environment tab)
   - Port binding issue (check startCommand)

3. If service shows "Deploy failed":
   - Check logs for specific error
   - Verify requirements-mirror.txt exists
   - Verify startCommand is correct

4. If service shows "Live" but still down:
   - Service might be crashing on startup
   - Check logs for Python exceptions
   - Check environment variables

5. Manual test from command line:
   curl -v https://rade-bot.onrender.com/healthz
""")

print("=" * 80)
