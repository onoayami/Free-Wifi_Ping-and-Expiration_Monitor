import re

with open("FreeWifi Connection Checker.py", "r", encoding="utf-8") as f:
    content = f.read()

# Extract toggle_display_mode method block
match = re.search(r'(\n    @rumps\.clicked\("⏱️ 表示切替 \(接続時間 ⇆ Ping\)"\)\n    def toggle_display_mode\(self, _\):\n(?:.*\n){22}        self\.update_display\(None\) # 表示を即時更新\n)', content)

if match:
    block = match.group(1)
    # Remove it from current location
    content = content.replace(block, "")
    
    # Insert it right before open_wifi_settings
    target = '\n    @rumps.clicked("🛜 Wi-Fi設定を開く")\n'
    content = content.replace(target, block + '\n' + target)
    
    with open("FreeWifi Connection Checker.py", "w", encoding="utf-8") as f:
        f.write(content)
    print("Success")
else:
    print("Not found")

