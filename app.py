import os
import csv
import json
import uuid
import msal
import datetime
import tempfile
import requests
import asyncio
import logging
from dotenv import load_dotenv

import openai
from quart import Quart, request, jsonify
from quart_cors import cors
from quart.logging import default_handler
from PIL import Image
from text_to_speech import save
from moviepy.editor import VideoFileClip, concatenate_videoclips

from pymongo import MongoClient, errors
from pymongo.errors import ConnectionFailure, OperationFailure

from botbuilder.schema import Activity, ActivityTypes
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, TurnContext, ConversationState, MemoryStorage, CardFactory, MessageFactory
from botbuilder.dialogs import DialogSet, DialogTurnStatus, WaterfallDialog, WaterfallStepContext, TextPrompt, DialogTurnResult

load_dotenv()
app = Quart(__name__)
cors(app)

client = openai.Client(api_key=os.getenv("OPENAI_API_KEY"))

# Initialize bot framework adapter
MICROSOFT_APP_ID = os.getenv("MICROSOFT_APP_ID")
MICROSOFT_APP_PASSWORD = os.getenv("MICROSOFT_APP_PASSWORD")
SETTINGS = BotFrameworkAdapterSettings(
    app_id=MICROSOFT_APP_ID, app_password=MICROSOFT_APP_PASSWORD
)
adapter = BotFrameworkAdapter(SETTINGS)
memory = MemoryStorage()
conversation_state = ConversationState(memory)

conversation_references = {}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('quart.app')
logger.addHandler(default_handler)


class MyBot:
    def __init__(self, conversation_state: ConversationState):
        self.conversation_state = conversation_state
        self.dialog_state = self.conversation_state.create_property("DialogState")
        self.user_state = self.conversation_state.create_property("UserProfile")
        self.dialogs = DialogSet(self.dialog_state)
        self.dialogs.add(
            WaterfallDialog(
                "mainDialog", [self.process_request, self.process_request_end]
            )
        )
        self.dialogs.add(
            WaterfallDialog(
                "adminDialog", [self.prompt_password, self.verify_password, self.fetch_and_send_data]
            )
        )
        self.dialogs.add(
            TextPrompt("PasswordPrompt")
        )
        self.mongo_client = MongoClient(os.getenv("MONGODB_CONNECTION_STRING"))
        self.database_name = os.getenv("DATABASE_NAME")
        self.collection_name = os.getenv("COLLECTION_NAME")

    async def save_query_info(self, user_email, query, file_url):
        query_id = str(uuid.uuid4())
        try:
            database = self.mongo_client[self.database_name]
            collection = database[self.collection_name]
            retries = 5
            while retries > 0:
                try:
                    query_info = {
                        "query_id": query_id,
                        "user_email": user_email,
                        "query": query,
                        "file_url": file_url,
                        "timestamp": datetime.datetime.utcnow()
                    }
                    collection.insert_one(query_info)
                    logger.info(f"Query info saved successfully: {query_info}")
                    break
                except (ConnectionFailure, OperationFailure) as e:
                    retries -= 1
                    logger.error(f"Error saving query info to MongoDB: {e}")
                    if retries == 0:
                        raise
                    await asyncio.sleep(2)
        except errors.PyMongoError as e:
            logger.error(f"MongoDB error after retries: {e}", exc_info=True)
        return query_id
        
    def get_user_email(self, aad_object_id):
        """
        Get user email using Azure AD object ID.
        """
        tenant_id = os.getenv("TENANT_ID")
        app = msal.ConfidentialClientApplication(
            os.getenv("MICROSOFT_APP_ID"),
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=os.getenv("MICROSOFT_APP_PASSWORD"),
        )
        graph_api_endpoint = os.getenv("GRAPH_API_ENDPOINT")
        def get_access_token():
            # Acquire a token from Azure AD
            result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
            if "access_token" in result:
                return result["access_token"]
            else:
                raise Exception("Could not obtain access token")
        try:
            if aad_object_id is None:
                return "test@gmail.com"
            else:
                access_token = get_access_token()
                headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
                user_info_url = f"{graph_api_endpoint}/users/{aad_object_id}"
                response = requests.get(user_info_url, headers=headers)

                if response.status_code == 200:
                    user_info = response.json()
                    return user_info.get("mail") or user_info.get("userPrincipalName")
                else:
                    raise Exception(f"Error fetching user info: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"Error fetching user email: {e}", exc_info=True)
            return "test@email.com"
        
    async def on_turn(self, turn_context: TurnContext):
        if turn_context.activity.type == ActivityTypes.message:
            dialog_context = await self.dialogs.create_context(turn_context)
            result = await dialog_context.continue_dialog()

            if result.status == DialogTurnStatus.Empty:
                if turn_context.activity.text is not None:
                    if turn_context.activity.text.lower() == "admin panel":
                        await dialog_context.begin_dialog("adminDialog")
                    else:
                        await dialog_context.begin_dialog("mainDialog")
                else:
                    await dialog_context.begin_dialog("mainDialog")

            await self.conversation_state.save_changes(turn_context)
        elif turn_context.activity.type == ActivityTypes.conversation_update:
            for member in turn_context.activity.members_added:
                if member.id != turn_context.activity.recipient.id:
                    await send_input_card(turn_context)

    async def prompt_password(self, step_context: WaterfallStepContext):
        password_card = {
            "type": "AdaptiveCard",
            "body": [
                {
                    "type": "TextBlock",
                    "text": "Please enter the admin password:",
                    "wrap": True,
                },
                {
                    "type": "Input.Text",
                    "id": "admin_password",
                    "placeholder": "Enter password",
                    "isPassword": True
                },
                {
                    "type": "ActionSet",
                    "actions": [
                        {
                            "type": "Action.Submit",
                            "title": "Submit",
                        }
                    ]
                }
            ],
            "actions": [],
            "version": "1.2",
        }
        card_attachment = CardFactory.adaptive_card(password_card)
        await step_context.context.send_activity(MessageFactory.attachment(card_attachment))
        return DialogTurnResult(DialogTurnStatus.Waiting) 
    
    async def verify_password(self, step_context: WaterfallStepContext):
        user_input = step_context.context.activity.value["admin_password"]
        ADMIN_PASSWORD = "ThisIsStrong123!"
        if user_input == ADMIN_PASSWORD:
            await step_context.context.send_activity("Password correct! Fetching data...")
            return await step_context.next(None)
        else:
            await step_context.context.send_activity("Incorrect password. Please try again.")
            return await step_context.end_dialog()
        
    async def fetch_and_send_data(self, step_context: WaterfallStepContext):
        try:
            database = self.mongo_client[self.database_name]
            collection = database[self.collection_name]
            data = list(collection.find())
            
            if data:
                # Convert data to CSV
                csv_file_path = tempfile.mktemp(suffix=".csv")
                keys = data[0].keys()
                with open(csv_file_path, 'w', newline='') as output_file:
                    dict_writer = csv.DictWriter(output_file, fieldnames=keys)
                    dict_writer.writeheader()
                    dict_writer.writerows(data)
                
                # Upload CSV to Azure Blob Storage
                csv_url = await upload_to_azure(csv_file_path, "Admin Panel", "admin@example.com")

                await step_context.context.send_activity(f"Here is the download link for the data: {csv_url}")
            else:
                await step_context.context.send_activity("No data found in the database.")
            
        except Exception as e:
            logging.error(f"Error fetching data: {e}")
            await step_context.context.send_activity("An error occurred while fetching data.")
        
        await step_context.end_dialog()
        await step_context.begin_dialog("mainDialog")

        return DialogTurnResult(DialogTurnStatus.Complete)

    
    async def process_request(self, step_context: WaterfallStepContext):
        turn_context = step_context.context
        user_profile = await self.user_state.get(turn_context, lambda: {})
        aad_object_id = getattr(turn_context.activity.from_property, 'id')
        user_email = self.get_user_email(aad_object_id)


        print("*************************************")
        print(aad_object_id)
        print("*************************************")

        if user_profile.get("request_in_progress"):
            await turn_context.send_activity(
                "Your previous request is still being processed. Please wait."
            )
            return await step_context.end_dialog()

        if (
            turn_context.activity.value
            and "query" in turn_context.activity.value
            and "generate_type" in turn_context.activity.value
        ):
            search_query = turn_context.activity.value["query"]
            generate_type = turn_context.activity.value["generate_type"]

            user_profile["request_in_progress"] = True
            await self.user_state.set(turn_context, user_profile)

            if generate_type == "Video":
                # Save conversation reference
                conversation_reference = TurnContext.get_conversation_reference(
                    turn_context.activity
                )
                await save_conversation_reference(
                    turn_context.activity.conversation.id, conversation_reference
                )

                # Acknowledge user request
                await turn_context.send_activity(
                    "Your video is being generated. This may take a few minutes. You will receive a notification once it's ready."
                )

                # Start video generation asynchronously
                asyncio.create_task(
                    self.generate_and_send_video(search_query, conversation_reference, user_email)
                )
            elif generate_type == "Image":
                # Process image generation normally
                generated_image_path = await generate_image(search_query)
                image_url = await upload_to_azure(generated_image_path, search_query, user_email)
                await turn_context.send_activity(
                    f"Here is your generated image: {image_url}"
                )
                await send_input_card(turn_context)
        else:
            await send_input_card(turn_context)

        return await step_context.next(None)

    async def process_request_end(self, step_context: WaterfallStepContext):
        user_profile = await self.user_state.get(step_context.context, lambda: {})
        user_profile["request_in_progress"] = False
        await self.user_state.set(step_context.context, user_profile)
        return await step_context.end_dialog()

    async def generate_and_send_video(self, search_query, conversation_reference, user_email):
        try:
            video_urls = await fetch_videos(search_query)
            merged_video_path = merge_videos(video_urls, search_query)
            video_url = await upload_to_azure(merged_video_path, search_query, user_email)

            # Send the video URL proactively
            await self.send_proactive_message(
                conversation_reference, f"Here is your merged video: {video_url}"
            )
        except Exception as e:
            # Handle exceptions and notify user
            await self.send_proactive_message(
                conversation_reference,
                f"An error occurred while generating the video: {str(e)}",
            )
        finally:
            # Reset the request_in_progress flag
            turn_context = TurnContext(adapter, conversation_reference)
            user_profile = await self.user_state.get(turn_context, lambda: {})
            user_profile["request_in_progress"] = False
            await self.user_state.set(turn_context, user_profile)


    async def send_proactive_message(self, conversation_reference, message):
        proactive_adapter = BotFrameworkAdapter(SETTINGS)

        async def callback(turn_context: TurnContext):
            await turn_context.send_activity(message)

        try:
            await proactive_adapter.continue_conversation(
                conversation_reference, callback, MICROSOFT_APP_ID
            )
        except Exception as e:
            logging.error(f"Error sending proactive message: {e}")


async def save_conversation_reference(conversation_id, conversation_reference):
    conversation_references[conversation_id] = conversation_reference


async def send_input_card(turn_context: TurnContext):
    card = {
        "type": "AdaptiveCard",
        "body": [
            {
                "type": "TextBlock",
                "text": "Please provide a query and select what you want to generate:",
                "wrap": True,
            },
            {
                "type": "Input.Text",
                "id": "query",
                "placeholder": "Enter your query here",
            },
            {
                "type": "ActionSet",
                "actions": [
                    {
                        "type": "Action.Submit",
                        "title": "Generate Image",
                        "data": {"generate_type": "Image"},
                    },
                    {
                        "type": "Action.Submit",
                        "title": "Generate Video",
                        "data": {"generate_type": "Video"},
                    },
                ],
            },
        ],
        "actions": [],
        "version": "1.2",
    }
    card_attachment = CardFactory.adaptive_card(card)
    await turn_context.send_activity(MessageFactory.attachment(card_attachment))


bot = MyBot(conversation_state)


def add_padding(base64_string):
    """Adds padding to a base64 string if necessary."""
    padding_needed = 4 - (len(base64_string) % 4)
    if padding_needed and padding_needed != 4:
        base64_string += "=" * padding_needed
    return base64_string


async def generate_image(search_query):
    openai_api_key = os.getenv("OPENAI_API_KEY")
    headers = {
        "Authorization": f"Bearer {openai_api_key}",
        "Content-Type": "application/json",
    }
    data = {"prompt": search_query, "n": 1, "size": "1024x1024"}

    response = requests.post(
        "https://api.openai.com/v1/images/generations", headers=headers, json=data
    )

    if response.status_code != 200:
        raise Exception(f"Failed to generate image: {response.text}")

    response_data = response.json()
    image_url = response_data["data"][0]["url"]

    # Download the generated image
    image_response = requests.get(image_url)
    if image_response.status_code != 200:
        raise Exception(f"Failed to download image: {image_response.text}")

    # Save the image to a temporary file
    temp_image = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    temp_image.write(image_response.content)
    temp_image.close()

    generated_image = Image.open(temp_image.name)
    logo = Image.open("logo.png")

    # Resize the logo (optional, you can adjust the size as needed)
    logo_size = (100, 100)  # Change this as needed
    logo.thumbnail(logo_size, Image.ANTIALIAS)


    # Calculate position for the logo (bottom-right corner)
    image_width, image_height = generated_image.size
    logo_width, logo_height = logo.size
    position = (image_width - logo_width, image_height - logo_height)

    # Paste the logo onto the generated image
    generated_image.paste(logo, position, logo)

    # Save the final image with the logo to a temporary file
    final_image = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    generated_image.save(final_image.name)
    final_image.close()

    return final_image.name


async def fetch_videos(search_query):
    headers = {
        "Authorization": f'{os.getenv("PEXEL_API_KEY")}',
    }

    response = requests.get(
        "https://api.pexels.com/videos/search",
        params={"query": search_query, "per_page": 5},
        headers=headers,
    )

    if response.status_code != 200:
        raise Exception(f"Failed to fetch videos: {response.text}")

    video_data = response.json()
    video_urls = [video["video_files"][0]["link"] for video in video_data["videos"]]
    return video_urls


async def generate_audio(text, output_filename):
    language = "en"
    try:
        save(text, language, file=output_filename)
    except Exception as e:
        logging.error(f"Error in text-to-speech conversion: {e}")
        raise


async def generate_script(topic):
    prompt = """You are a seasoned content writer for a YouTube Shorts channel, specializing in facts videos. 
        Your facts shorts are concise, each lasting less than 50 seconds (approximately 140 words). 
        They are incredibly engaging and original. When a user requests a specific type of facts short, you will create it.

        For instance, if the user asks for:
        Weird facts
        You would produce content like this:

        Weird facts you don't know:
        - Bananas are berries, but strawberries aren't.
        - A single cloud can weigh over a million pounds.
        - There's a species of jellyfish that is biologically immortal.
        - Honey never spoils; archaeologists have found pots of honey in ancient Egyptian tombs that are over 3,000 years old and still edible.
        - The shortest war in history was between Britain and Zanzibar on August 27, 1896. Zanzibar surrendered after 38 minutes.
        - Octopuses have three hearts and blue blood.

        You are now tasked with creating the best short script based on the user's requested type of 'facts'.

        Keep it brief, highly interesting, and unique.

        Stictly output the script in a JSON format like below, and only provide a parsable JSON object with the key 'script'.

        # Output
        {"script": "Here is the script ..."}
        """
    client = openai.Client(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": topic},
        ],
    )
    content = response.choices[0].message.content
    print(content)
    try:
        # Remove any newlines or other control characters from the response
        sanitized_content = (
            content.replace("\n", " ").replace("\r", " ").replace("\t", " ")
        )
        # Attempt to directly parse the sanitized content
        script = json.loads(sanitized_content)["script"]
    except json.JSONDecodeError as e:
        logging.error(f"JSON decode error: {e}")
        # Try to extract the JSON object from the content manually
        try:
            json_start_index = sanitized_content.find("{")
            json_end_index = sanitized_content.rfind("}")
            sanitized_content = sanitized_content[json_start_index : json_end_index + 1]
            script = json.loads(sanitized_content)["script"]
        except Exception as inner_e:
            logging.error(f"Failed to extract script: {inner_e}")
            script = "Error generating script. Please try again."
    except KeyError as e:
        logging.error(f"KeyError: {e}")
        script = "Error generating script. Please try again."
    print(script)
    return script


def merge_videos(video_urls, search_query):
    clips = []
    target_width = 1280
    target_height = 720
    for url in video_urls:
        response = requests.get(url)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as temp_video:
            temp_video.write(response.content)
            temp_video_path = temp_video.name
            clip = VideoFileClip(temp_video_path)
            clip = clip.resize(newsize=(target_width, target_height))
            clips.append(clip)

    if not clips:
        raise Exception("No suitable video clips found.")

    # text_script = generate_script(search_query)

    # try:
    #     audio_file_path = tempfile.mktemp(suffix='.mp3')
    #     generate_audio(text_script, audio_file_path)
    #     audio_clip = AudioFileClip(audio_file_path)
    # except Exception as e:
    #     logging.error(f"Error generating audio: {e}")
    #     raise

    # audio_duration = audio_clip.duration
    # total_video_duration = sum(clip.duration for clip in clips)

    # if total_video_duration < audio_duration:
    #     # Repeat the video clips to match the audio duration
    #     repeated_clips = []
    #     current_duration = 0
    #     while current_duration < audio_duration:
    #         for clip in clips:
    #             if current_duration + clip.duration > audio_duration:
    #                 remaining_duration = audio_duration - current_duration
    #                 repeated_clips.append(clip.subclip(0, remaining_duration))
    #                 current_duration += remaining_duration
    #                 break
    #             repeated_clips.append(clip)
    #             current_duration += clip.duration
    #     clips = repeated_clips
    # else:
    #     # Trim the video clips to match the audio duration
    #     cumulative_duration = 0
    #     for i in range(len(clips)):
    #         if cumulative_duration + clips[i].duration > audio_duration:
    #             clips[i] = clips[i].subclip(0, audio_duration - cumulative_duration)
    #             clips = clips[:i+1]  # Keep only the clips up to this point
    #             break
    #         cumulative_duration += clips[i].duration

    final_clip = concatenate_videoclips(clips, method="compose")
    # final_clip = final_clip.set_audio(audio_clip)
    merged_video_path = tempfile.mktemp(suffix=".mp4")
    final_clip.write_videofile(merged_video_path, codec="libx264", audio_codec="aac")
    return merged_video_path


async def upload_to_azure(file_path, search_query, user_email):

    connect_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")

    container_name = os.getenv("AZURE_STORAGE_CONTAINER_NAME")

    blob_service_client = BlobServiceClient.from_connection_string(connect_str)
    blob_client = blob_service_client.get_blob_client(
        container=container_name, blob=os.path.basename(file_path)
    )

    with open(file_path, "rb") as data:
        blob_client.upload_blob(data, overwrite=True)

    sas_token = generate_blob_sas(
        account_name=blob_client.account_name,
        container_name=blob_client.container_name,
        blob_name=blob_client.blob_name,
        account_key=blob_service_client.credential.account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.datetime.utcnow() + datetime.timedelta(days=365 * 10),
    )

    file_url = f"{blob_client.url}?{sas_token}"
    await bot.save_query_info(user_email, search_query, file_url)

    return file_url


@app.route("/api/messages", methods=["POST"])
async def messages():
    if "application/json" in request.headers["Content-Type"]:
        body = await request.json
    else:
        return jsonify({"message": "Invalid content type"}), 415

    activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization", "")

    try:
        await adapter.process_activity(activity, auth_header, bot.on_turn)
        return "", 200
    except Exception as e:
        logging.error(f"Error processing activity: {e}")
        return jsonify({"message": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
