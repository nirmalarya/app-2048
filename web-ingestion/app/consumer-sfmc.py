#!/usr/bin/env python
#
# Kafka Consumer.
# Connects to local and non-prod Kafka brokers, retrieves messages from a topic, prints them,
# sends them to SFMC if type is "stream", and exports events to S3 if they type is "bulk". 
# For type bulk, events for the same JourneyID are collected for 2 minutes before they are stored in a CSV and pushed to S3.
# All Secrets, S3 and SFMC API config are expected to exist in Vault.
# Signal handlers for SIGINT (interrupt signal) and SIGTERM (termination signal) are used to handle graceful shutdowns.

from confluent_kafka import Consumer, KafkaException
from dotenv import load_dotenv
import hvac
import os
import threading
import time
import tempfile
import logging
import signal
import sys
import requests
import json
import boto3
import csv
from collections import defaultdict

# Load environment variables from .env file
load_dotenv()

# Set up logging for Kafka client
logging.basicConfig(level=logging.DEBUG)

# Define HashiCorp Vault address and AppRole details
vault_addr = os.getenv('VAULT_ADDR')
vault_role_id = os.getenv('VAULT_ROLE_ID')
vault_secret_id = os.getenv('VAULT_SECRET_ID')
vault_mount_point = 'kv'
vault_secret_path = 'ph-commercial-architecture/non-prod/edh'
vault_secret_path_sfmc = 'ph-commercial-architecture/non-prod/sfmc'

if not vault_role_id or not vault_secret_id:
    raise ValueError("VAULT_ROLE_ID and VAULT_SECRET_ID environment variables must be set")

# Function to authenticate with Vault using AppRole
def authenticate_with_approle(vault_addr, role_id, secret_id):
    client = hvac.Client(url=vault_addr)
    auth_response = client.auth.approle.login(
        role_id=role_id,
        secret_id=secret_id
    )
    return auth_response['auth']['client_token']

# Function to renew the Vault token periodically
def renew_vault_token_periodically(client, interval=7 * 3600):
    while True:
        time.sleep(interval)
        try:
            client.renew_self()
            print("Vault token renewed successfully")
        except Exception as e:
            print(f"Error renewing Vault token: {e}")

# Authenticate with Vault and start the token renewal thread
vault_token = authenticate_with_approle(vault_addr, vault_role_id, vault_secret_id)
vault_client = hvac.Client(url=vault_addr, token=vault_token)

vault_renew_thread = threading.Thread(target=renew_vault_token_periodically, args=(vault_client,))
vault_renew_thread.daemon = True
vault_renew_thread.start()

# Function to get HashiCorp Vault secrets
def get_vault_secrets(client, vault_mount_point, vault_secret_path):
    try:
        secrets = client.secrets.kv.v2.read_secret_version(
            path=vault_secret_path,
            mount_point=vault_mount_point
        )
        return secrets['data']['data']
    except hvac.exceptions.Forbidden as e:
        raise Exception(f"Permission denied: {e}")
    except hvac.exceptions.InvalidPath as e:
        raise Exception(f"Invalid path: {e}")
    except Exception as e:
        raise Exception(f"Error retrieving secrets from Vault: {e}")

# Get the Vault secrets
secrets = get_vault_secrets(vault_client, vault_mount_point, vault_secret_path)

# function to get HashiCorp Vault secrets for SFMC
def get_vault_secrets_sfmc(client, vault_mount_point, vault_secret_path_sfmc):
    try:
        secrets = client.secrets.kv.v2.read_secret_version(
            path=vault_secret_path_sfmc,
            mount_point=vault_mount_point
        )
        return secrets['data']['data']
    except hvac.exceptions.Forbidden as e:
        raise Exception(f"Permission denied: {e}")
    except hvac.exceptions.InvalidPath as e:
        raise Exception(f"Invalid path: {e}")
    except Exception as e:
        raise Exception(f"Error retrieving secrets from Vault: {e}")
    
# Get the Vault secrets for SFMC
secrets_sfmc = get_vault_secrets_sfmc(vault_client, vault_mount_point, vault_secret_path_sfmc)


# Define Kafka broker host and port
kafka_broker = secrets['kafka_broker_us']

# Get the sfmc_auth_endpoint from environment variable
#sfmc_auth_endpoint = os.getenv('SFMC_AUTH_ENDPOINT')
#print (f"sfmc_auth_endpoint: {sfmc_auth_endpoint}")
# Salesforce Marketing Cloud details
sfmc_auth_endpoint = secrets_sfmc['sfmc_auth_endpoint']
sfmc_api_endpoint = secrets_sfmc['sfmc_api_endpoint']
sfmc_client_id = secrets_sfmc['sfmc_client_id']
sfmc_client_secret = secrets_sfmc['sfmc_client_secret']
sfmc_account_id = secrets_sfmc['sfmc_account_id']

# AWS S3 details
##s3_bucket_name = secrets['s3_bucket_name']
##aws_access_key_id = secrets['aws_access_key_id']
##aws_secret_access_key = secrets['aws_secret_access_key']
##aws_region = secrets['aws_region']

# Initialize S3 client
##s3_client = boto3.client(
##    's3',
##    aws_access_key_id=aws_access_key_id,
##    aws_secret_access_key=aws_secret_access_key,
##    region_name=aws_region
##)

# Global variable to hold the SFMC access token
sfmc_access_token = None

# Dictionary to hold bulk event data and timers
bulk_events = defaultdict(list)
bulk_timers = {}

# Lock for thread-safe operations on bulk_events and bulk_timers
bulk_lock = threading.Lock()

# Function to authenticate with SFMC
def authenticate_with_sfmc():
    global sfmc_access_token
    auth_payload = {
        "grant_type": "client_credentials",
        "client_id": sfmc_client_id,
        "client_secret": sfmc_client_secret,
        "account_id": sfmc_account_id
    }
    #Sam Added this for testing to skip the SSL verification ERROR   
    #response = requests.post(sfmc_auth_endpoint, json=auth_payload)
    response =  requests.post(sfmc_auth_endpoint, json=auth_payload, verify=False)
    response_data = response.json()
    if response.status_code == 200:
        sfmc_access_token = response_data['access_token']
        print("SFMC access token obtained successfully")
    else:
        raise Exception(f"Error obtaining SFMC access token: {response_data}")

# Function to renew the SFMC token periodically
def renew_sfmc_token_periodically(interval=15 * 60):
    while True:
        time.sleep(interval)
        try:
            authenticate_with_sfmc()
        except Exception as e:
            print(f"Error renewing SFMC access token: {e}")

# Authenticate with SFMC and start the token renewal thread
authenticate_with_sfmc()
sfmc_renew_thread = threading.Thread(target=renew_sfmc_token_periodically)
sfmc_renew_thread.daemon = True
sfmc_renew_thread.start()

# Create temporary files for SSL certificates
with tempfile.NamedTemporaryFile(delete=False) as temp_cafile, \
     tempfile.NamedTemporaryFile(delete=False) as temp_certfile, \
     tempfile.NamedTemporaryFile(delete=False) as temp_keyfile:

    temp_cafile.write(secrets['ssl_ca'].encode('utf-8'))
    temp_certfile.write(secrets['ssl_cert'].encode('utf-8'))
    temp_keyfile.write(secrets['ssl_key'].encode('utf-8'))

    temp_cafile.flush()
    temp_certfile.flush()
    temp_keyfile.flush()

    # Kafka consumer configuration
    conf = {
        'bootstrap.servers': kafka_broker,
        'security.protocol': 'SSL',
        'ssl.ca.location': temp_cafile.name,
        'ssl.certificate.location': temp_certfile.name,
        'ssl.key.location': temp_keyfile.name,
        'ssl.key.password': secrets['ssl_key_pass'],
        'group.id': 'fastapi-consumer-group',
        'auto.offset.reset': 'earliest'
    }

    # Initialize Kafka consumer
    consumer = Consumer(conf)

    # Subscribe to the topic
    consumer.subscribe(['app.ph-commercial.website.click.events.avro'])

    def consume_messages():
        try:
            while True:
                msg = consumer.poll(timeout=1.0)
                if msg is None:
                    continue
                if msg.error():
                    if msg.error().code() == KafkaException._PARTITION_EOF:
                        logging.debug(f"End of partition reached {msg.partition()}")
                    elif msg.error():
                        raise KafkaException(msg.error())
                else:
                    event_data = json.loads(msg.value().decode('utf-8'))
                    logging.info(f"Received message: {event_data}")
                    handle_event(event_data)
        except Exception as e:
            logging.error(f"Error while consuming messages: {e}")
        finally:
            consumer.close()

    def handle_event(event_data):
        event_type = event_data.get('type')
        journey_id = event_data.get('JourneyID')
        
        if event_type == 'stream':
            send_to_sfmc(json.dumps(event_data))
        elif event_type == 'bulk':
            add_bulk_event(journey_id, event_data)

    def send_to_sfmc(event_data):
        headers = {
            'Authorization': f'Bearer {sfmc_access_token}',
            'Content-Type': 'application/json'
        }
        response = requests.post(sfmc_api_endpoint, headers=headers, data=event_data)
        if response.status_code == 200:
            logging.info("Event sent to SFMC successfully")
        else:
            logging.error(f"Error sending event to SFMC: {response.status_code} {response.text}")

    def add_bulk_event(journey_id, event_data):
        with bulk_lock:
            if journey_id not in bulk_events:
                bulk_events[journey_id] = []
                bulk_timers[journey_id] = threading.Timer(120, export_bulk_events, [journey_id])
                bulk_timers[journey_id].start()
            bulk_events[journey_id].append(event_data)

    def export_bulk_events(journey_id):
        with bulk_lock:
            events = bulk_events.pop(journey_id, [])
            if journey_id in bulk_timers:
                bulk_timers.pop(journey_id).cancel()
        
        if events:
            csv_file_path = generate_csv(journey_id, events)
            upload_to_s3(csv_file_path, journey_id)

    def generate_csv(journey_id, events):
        csv_file_path = f'/tmp/{journey_id}.csv'
        with open(csv_file_path, 'w', newline='') as csvfile:
            fieldnames = events[0].keys()
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            for event in events:
                writer.writerow(event)
        return csv_file_path

    ##def upload_to_s3(file_path, journey_id):
    ##    try:
    ##        s3_client.upload_file(file_path, s3_bucket_name, f'{journey_id}.csv')
    ##        logging.info(f"Bulk events for JourneyID {journey_id} uploaded to S3 successfully")
    ##    except Exception as e:
    ##        logging.error(f"Error uploading file to S3: {e}")

    consume_thread = threading.Thread(target=consume_messages)
    consume_thread.daemon = True
    consume_thread.start()

    def signal_handler(sig, frame):
        print("Signal received, shutting down...")
        consumer.close()
        os.unlink(temp_cafile.name)
        os.unlink(temp_certfile.name)
        os.unlink(temp_keyfile.name)
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Keep the main thread alive to let the consumer thread run
    signal.pause()