import warnings

# Suppress the blessed terminal capability warnings
warnings.filterwarnings(
    "ignore",
    message="unknown terminal capability",
    category=UserWarning,
)

import requests
import win32gui
import psutil
import win32process
import threading
import time
from datetime import datetime
import json
import os
import ctypes
import base64
from blessed import Terminal
from pyfiglet import Figlet


isTracking = False

# Ensure a safe, writable app directory in the user's home folder
USER_HOME = os.path.expanduser("~")
APP_DIR = os.path.join(USER_HOME, ".noodli")
os.makedirs(APP_DIR, exist_ok=True)

JSON_FILE = os.path.join(APP_DIR, 'tracker.json')
CONFIG_FILE = os.path.join(APP_DIR, 'config.json')

LOGIN_URL = 'https://noodlibackend.onrender.com/api/token/'
REFRESH_URL = 'https://noodlibackend.onrender.com/api/token/refresh/'
VERIFY_URL = 'https://noodlibackend.onrender.com/api/token/verify/'

term = Terminal()


#   Only this session's usage lives here. The file is the source of truth for
#   previous days, so we must not load it back into this list (doing so made
#   save_merged_json re-add old totals on every tick).
session_usage = []
session_seconds = 0


def get_active_app():
    try:
        active_window = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(active_window)
        process = psutil.Process(pid)
        app_name = process.name()
        return app_name
    except Exception as e:
        print(f"Something went wrong - {e}")
        return None


def save_merged_json(new_entries):
    """Loads existing json, merges items matching name and date, and saves back."""
    try:
        with open(JSON_FILE, 'r') as file:
            existing_data = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        existing_data = []

    #   Create a lookup dictionary using (name, date) as a composite key
    merged_dict = {}
    for item in existing_data:
        key = (item.get("name"), item.get("date"))
        merged_dict[key] = item.copy()

    #   Merge new entries into the dictionary
    for new_item in new_entries:
        key = (new_item.get("name"), new_item.get("date"))

        if key in merged_dict:
            #   If the entry already exists for this name and date, combine numeric fields (like count)
            for field, value in new_item.items():
                if field in ["name", "date"]:
                    continue

                if field in merged_dict[key]:
                    if isinstance(value, (int, float)) and isinstance(merged_dict[key][field], (int, float)):
                        merged_dict[key][field] += value
                    else:
                        merged_dict[key][field] = value
                else:
                    merged_dict[key][field] = value
        else:
            #   If it doesn't exist, add it entirely
            merged_dict[key] = new_item.copy()

    #   Convert back to list and save with formatting
    updated_data = list(merged_dict.values())
    with open(JSON_FILE, 'w') as file:
        json.dump(updated_data, file, indent=4)


def calculate_app_session_usage():
    global isTracking
    while isTracking:
        app = get_active_app()
        found = False
        if not app:
            time.sleep(1)
            continue

        current_date = datetime.now().strftime("%Y-%m-%d")

        for entry in session_usage:
            if entry.get("name") == app and entry.get("date") == current_date:
                entry["count"] += 1
                found = True
                break

        if not found:
            session_usage.append({"name": app, "count": 1, "date": current_date})

        #   Persist only this tick's +1. Passing the whole session_usage here
        #   merged every previous second again, so the totals exploded.
        save_merged_json([{"name": app, "count": 1, "date": current_date}])
        time.sleep(1)


def start_app():
    global isTracking
    if isTracking:
        print("Tracking is already running.")
        return

    isTracking = True
    print("Tracking started.")

    tracking_thread = threading.Thread(target=calculate_app_session_usage, daemon=False)
    tracking_thread.start()


def stop_app():
    global isTracking
    if not isTracking:
        print("Tracking is not running.")
        return

    isTracking = False


def upload_to_cloud():
    global session_usage
    try:
        with open(JSON_FILE, 'r') as file:
            data = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        data = []
        
    try:
        jwt = load_token()
        if not jwt:
            print("No access token found. Please log in first.")
            return

        # Run the sync animation for 3 cycles cleanly
        for i in range(1, 4):
            print(term.clear_eol + f"\r{term.reverse('Syncing' + '.' * i)}", end="", flush=True)
            time.sleep(0.5)
        print(term.normal)

        res = requests.post(
            'https://noodlibackend.onrender.com/api/sessions/',
            json=data,
            headers={
                'Authorization': f"Bearer {jwt}",
            },
            timeout=10
        )

        print(res)

        if res.ok:
            #   Clear the tracker only after the server accepted the upload.
            with open(JSON_FILE, 'w') as file:
                json.dump([], file, indent=4)
            session_usage.clear()
            print("Sync successful. Tracker cleared.")
        else:
            print(f"Sync failed ({res.status_code}). Tracker left untouched.")
    except Exception as e:
        print(f"error- {e}")


def _set_hidden(path, hidden):
    if os.name != 'nt':
        return
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(path)
        if attrs == -1:
            return
        attrs &= ~0x01  
        if hidden:
            attrs = (attrs | 0x02) & ~0x80
        else:
            attrs &= ~0x02
            if attrs == 0:
                attrs = 0x80
        ctypes.windll.kernel32.SetFileAttributesW(path, attrs)
    except Exception:
        pass


def save_token(token, refresh=None):
    """Write the access (and refresh) token to a hidden config file."""
    if refresh is None:
        refresh = load_config().get('refresh')

    data = {'access': token}
    if refresh:
        data['refresh'] = refresh

    # Windows refuses to truncate a hidden/read-only file, so clear attributes while writing.
    _set_hidden(CONFIG_FILE, False)
    tmp = CONFIG_FILE + '.tmp'
    try:
        with open(tmp, 'w') as file:
            json.dump(data, file)
        os.replace(tmp, CONFIG_FILE)
    except PermissionError:
        # Fall back to a direct write if the atomic replace is blocked.
        with open(CONFIG_FILE, 'w') as file:
            json.dump(data, file)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    _set_hidden(CONFIG_FILE, True)


def load_config():
    try:
        with open(CONFIG_FILE, 'r') as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_token():
    return load_config().get('access')


def is_token_valid(token):
    """Check that the saved JWT access token has not expired."""
    if not token:
        return False

    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return False

    exp = data.get('exp')
    return bool(exp) and time.time() < exp


def is_token_verified(token):
    """Ask the server whether the token is still valid.

    Falls back to True on network errors so an offline user is not forced to log in.
    """
    if not token:
        return False
    try:
        res = requests.post(VERIFY_URL, json={'token': token}, timeout=5)
    except requests.RequestException:
        return True

    if res.status_code == 200:
        return True
    if res.status_code in (400, 401, 403):
        return False
    return True


def refresh_access(refresh):
    """Exchange a refresh token for a new access token."""
    if not refresh:
        return None
    try:
        res = requests.post(REFRESH_URL, json={'refresh': refresh}, timeout=10)
        data = res.json()
    except (requests.RequestException, ValueError):
        return None

    return data.get('access') or data.get('access_token')


def login(email, password):
    """Request an access token from the API. Returns the response JSON."""
    try:
        res = requests.post(
            LOGIN_URL,
            json={'email': email, 'password': password},
            timeout=10,
        )
        data = res.json()
    except requests.RequestException as e:
        return {'detail': str(e)}
    except ValueError:
        return {'detail': 'Invalid response from server'}

    if 'access' in data:
        return {'access': data['access'], 'refresh': data.get('refresh')}
    if 'access_token' in data:
        return {'access': data['access_token'], 'refresh': data.get('refresh_token')}
    if 'token' in data:
        return {'access': data['token'], 'refresh': data.get('refresh')}

    return data


def run_term():
    """Render the login screen and return the access token on success."""
    f = Figlet(font='big')

    with term.fullscreen(), term.cbreak(), term.hidden_cursor():
        email = ''
        password = ''
        is_email = True
        message = ''

        while True:
            # Clear screen and move to top-left corner
            print(term.home + term.clear, end='')

            # Render ASCII banner
            print(f.renderText('N O O D L I'))
            print('=' * 60)

            # Display current state
            if is_email:
                print(f"Input your email:    {email}_")
                print(f"Password:            {'*' * len(password)}")
            else:
                print(f"Input your email:    {email}")
                print(f"Password:            {'*' * len(password)}_")

            if message:
                print(f"\n{message}")

            print('\n(Press ENTER to switch fields, ESC to exit)')

            key = term.inkey()

            # Handle exit
            if key.name == 'KEY_ESCAPE':
                return None

            # Handle field switching / login submission
            elif key.name in ('KEY_ENTER', 'KEY_RETURN'):
                if is_email and email.strip():
                    is_email = False
                elif not is_email:
                    result = login(email, password)

                    if 'access' in result:
                        save_token(result['access'], result.get('refresh'))
                        return result['access']

                    message = f"Login failed: {result.get('detail', 'unknown error')}"

            # Handle Backspace
            elif key.name in ('KEY_BACKSPACE', 'KEY_DELETE'):
                if is_email and len(email) > 0:
                    email = email[:-1]
                elif not is_email and len(password) > 0:
                    password = password[:-1]

            elif key and not key.is_sequence:
                if is_email:
                    email += key
                else:
                    password += key


def main():
    config = load_config()
    token = config.get('access')
    refresh = config.get('refresh')

    if is_token_valid(token) and is_token_verified(token):
        print("Existing access token is valid. Skipping login.")
    else:
        # Try to renew the session silently before asking for credentials.
        new_access = refresh_access(refresh)
        if new_access and is_token_valid(new_access):
            save_token(new_access, refresh)
            token = new_access
            print("Session refreshed. Skipping login.")
        else:
            token = run_term()
            if not token:
                return
            print("Login successful. Access token saved.")

    start_app()

    try:
        while isTracking:
            command = input("Type 'stop' to stop or 'sync' to upload: ").strip().lower()

            if command == 'stop':
                stop_app()
            elif command == 'sync':
                upload_to_cloud()
    except (KeyboardInterrupt, EOFError):
        stop_app()


if __name__ == "__main__":
    main()