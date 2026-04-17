import requests
import time

API_URL = "http://127.0.0.1:8000"

def test_live_graph():
    print("🤖 1. Agent writes initial data and formula to C3, D3, and E3...")
    payload_1 = {
        "agent_id": "Finance-Bot",
        "target_start_a1": "C3", 
        "data_payload": [ [100, 50, "=SUM(C3, D3)"] ],
        "shift_direction": "right"
    }
    requests.post(f"{API_URL}/agent/write", json=payload_1)
    
    grid = requests.get(f"{API_URL}/debug/grid").json()
    print(f"📊 Initial State - C3: {grid['C3']['value']}, D3: {grid['D3']['value']} -> E3 Total: {grid['E3']['value']}")

    print("\n⏳ Waiting 2 seconds...")
    time.sleep(2)

    print("\n🤖 2. Agent updates ONLY cell C3 to 900...")
    payload_2 = {
        "agent_id": "Correction-Bot",
        "target_start_a1": "C3", 
        "data_payload": [ [900] ], # Only overwriting C3
        "shift_direction": "right"
    }
    requests.post(f"{API_URL}/agent/write", json=payload_2)

    print("\n✨ Checking if E3 magically updated itself...")
    grid = requests.get(f"{API_URL}/debug/grid").json()
    print(f"📊 Final State - C3: {grid['C3']['value']}, D3: {grid['D3']['value']} -> E3 Total: {grid['E3']['value']}")

if __name__ == "__main__":
    test_live_graph()