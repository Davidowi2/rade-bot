"""Test the Render service healthz endpoint."""
import requests

url = "https://rade-bot.onrender.com/healthz"

print(f"Testing: {url}")
print("-" * 60)

try:
    response = requests.get(url, timeout=15)
    print(f"Status Code: {response.status_code}")
    print(f"Response Headers:")
    for key, value in response.headers.items():
        print(f"  {key}: {value}")
    print(f"\nResponse Body:")
    print(response.text)
except requests.exceptions.Timeout:
    print("ERROR: Request timed out (service likely spinning up)")
except requests.exceptions.ConnectionError as e:
    print(f"ERROR: Connection failed - {e}")
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")
