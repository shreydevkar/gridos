import requests

API_URL = "http://127.0.0.1:8000"

def test_formulas():
    print("🧪 Testing the Formula Registry...")

    # Test 1: A valid SUM function
    payload_sum = {
        "function_name": "SUM",
        "arguments": [15, 25, 10]
    }
    response_sum = requests.post(f"{API_URL}/formula/evaluate", json=payload_sum)
    print(f"\nTesting SUM(15, 25, 10):")
    print(response_sum.json())

    # Test 2: A valid MAX function
    payload_max = {
        "function_name": "MAX",
        "arguments": [5, 99, 21, 42]
    }
    response_max = requests.post(f"{API_URL}/formula/evaluate", json=payload_max)
    print(f"\nTesting MAX(5, 99, 21, 42):")
    print(response_max.json())

    # Test 3: An unknown function (should fail gracefully)
    payload_unknown = {
        "function_name": "MAGIC_AI_PREDICT",
        "arguments": [1, 2, 3]
    }
    response_unknown = requests.post(f"{API_URL}/formula/evaluate", json=payload_unknown)
    print(f"\nTesting Unknown Function:")
    print(response_unknown.json())

if __name__ == "__main__":
    test_formulas()