import requests
import datetime
from api_keys import load_kalshi_rsa_private_key, sign_pss_text

# Get the current time
current_time = datetime.datetime.now()

# Convert the time to a timestamp (seconds since the epoch)
timestamp = current_time.timestamp()

# Convert the timestamp to milliseconds
current_time_milliseconds = int(timestamp * 1000)
timestampt_str = str(current_time_milliseconds)

# Load the RSA private key
private_key = load_kalshi_rsa_private_key()

method = "GET"
base_url = 'https://api.elections.kalshi.com'
path='/trade-api/v2/portfolio/balance'

msg_string = timestampt_str + method + path

sig = sign_pss_text(private_key, msg_string)

headers = {
        'KALSHI-ACCESS-KEY': 'bd04cf2c-f84a-4744-98c2-7f18455848fa',
        'KALSHI-ACCESS-SIGNATURE': sig,
        'KALSHI-ACCESS-TIMESTAMP': timestampt_str
    }
response = requests.get(base_url + path, headers=headers)
print("Status Code:", response.status_code)
print("Response Body:", response.text)