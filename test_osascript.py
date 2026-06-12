import subprocess
items = ["【すべて解除】", "SSID1", "SSID2"]
items_literal = ", ".join(f'"{s}"' for s in items)
osascript_cmd = f'''
tell application "System Events"
    activate
    set chosen to choose from list {{{items_literal}}} with title "非通知wifiリストの解除" with prompt "解除するWi-Fiを選択してください（複数選択可）" with multiple selections allowed
    if chosen is false then
        return "__CANCELLED__"
    end if
    set AppleScript's text item delimiters to linefeed
    return chosen as text
end tell
'''
result = subprocess.run(["osascript", "-e", osascript_cmd], capture_output=True, text=True)
print(result.stdout)
