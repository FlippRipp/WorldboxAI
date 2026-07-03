import asyncio
import os
import websockets
import json
import urllib.request

PORT = os.environ.get("WB_PORT", "8321")

async def test_websocket():
    uri = f"ws://localhost:{PORT}/ws/chat"
    try:
        async with websockets.connect(uri) as websocket:
            print("Connected to WebSocket.")
            
            # Send a dummy message
            payload = {"action": "turn", "text": "I swing my sword"}
            await websocket.send(json.dumps(payload))
            print(f"Sent: {payload}")
            
            # Receive streaming tokens
            print("Receiving streaming output: ", end="", flush=True)
            while True:
                response = await websocket.recv()
                data = json.loads(response)
                
                if data["type"] == "token":
                    print(data["content"], end="", flush=True)
                elif data["type"] == "done":
                    print("\n\n[Done] Final State received:")
                    print(json.dumps(data["state"], indent=2))
                    break
                    
    except Exception as e:
        print(f"WebSocket error: {e}")

def test_asset_mount():
    url = f"http://localhost:{PORT}/assets/wb_core_combat/test_image.png"
    print(f"\nTesting Asset Mount: {url}")
    try:
        response = urllib.request.urlopen(url)
        content = response.read().decode('utf-8')
        print(f"Asset fetched successfully. Content: '{content}'")
    except Exception as e:
        print(f"Asset fetch failed: {e}")

async def main():
    await test_websocket()
    test_asset_mount()

if __name__ == "__main__":
    asyncio.run(main())
