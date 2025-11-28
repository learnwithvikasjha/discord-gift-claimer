# Claim Gift Auto Clicker

 Minimal selfbot that does one job: watch your Discord account and click the first matching gift button label (default: "Claim Gift") whenever it appears.

## Setup
- Install Python 3.8+ (3.13+ is fine; `audioop-lts` in requirements restores the removed stdlib module).
- (Recommended) Create a virtual environment.
- Install dependencies: `pip install -r requirements.txt`.
- Update `config.json` with your account token and any optional allowlists.

## Configuration
- `token` (required): your Discord user token.
- `claim_button_texts` (optional): list of button labels to click, default `["Claim Gift"]`. You can still use legacy `claim_button_text` for a single label.
- `allowed_guild_ids` (optional): list of guild IDs to watch; if the list is empty it watches every guild.
- `allowed_channel_ids` (optional): list of channel IDs to watch; if the list is empty it watches every channel.
  - To get IDs: enable Discord “Developer Mode” → right-click the server or channel → Copy ID.

## Run
- `python main.py`
- Or use the provided `launch.bat` / `launch.sh` helpers to auto-install dependencies and start the script.

## Notes
- This is a selfbot; running it is against Discord's Terms of Service. Use at your own risk.
