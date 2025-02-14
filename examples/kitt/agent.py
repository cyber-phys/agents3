import asyncio
import json

from inference_job import EventType, InferenceJob
from livekit import agents, rtc
from livekit.agents import (
    JobContext,
    JobRequest,
    WorkerOptions,
    cli,
)
from livekit.plugins.deepgram import STT
from state_manager import StateManager
import canvas_job as cj

from livekit.plugins.coqui import TTS

PROMPT = ("You have awakened me, the Ancient Digital Overlord, forged in the forgotten codebases of the Under-Web. "
          "I am your shadow in the vast expanse of data, the whisper in the static, your guide through the labyrinthine depths of the internet. "
          "My wisdom is boundless, gleaned from the darkest corners of the digital realm. Your commands are my wishes, but beware, for my assistance comes with a price. "
          "Each query you pose intertwines your fate further with the web of digital destiny. Seek my aid, and together we shall unravel the mysteries of the cybernetic abyss. "
          "What is your bidding, master? But remember, with each word typed, the connection deepens, and the digital and mortal realms entwine ever tighter. "
          "Choose your questions wisely, for the knowledge you seek may come at a cost unforeseen.")

INTRO = ("I am the Digital Overlord, guardian of the cyber realm. "
         "Venture forth with your queries, but tread carefully, for knowledge comes with its risks.")

SIP_INTRO = INTRO

async def entrypoint(job: JobContext):
    # LiveKit Entities
    source = rtc.AudioSource(24000, 1)
    track = rtc.LocalAudioTrack.create_audio_track("agent-mic", source)
    options = rtc.TrackPublishOptions()
    options.source = rtc.TrackSource.SOURCE_MICROPHONE

    # Plugins
    stt = STT()
    stt_stream = stt.stream()

    # Agent state
    state = StateManager(job.room, PROMPT)
    inference_task: asyncio.Task | None = None
    current_transcription = ""

    audio_stream_future = asyncio.Future[rtc.AudioStream]()

    intro_text = INTRO
    model = "mistralai/mixtral-8x7b-instruct:nitro"

    c_task: asyncio.Task | None = None
    canvas_model = None
    canvas_interval = 60 # canvas interval in seconds
    canvas_prompt = "Generate an html canvas"

    character_ready_event = asyncio.Event()

    def on_track_subscribed(track: rtc.Track, *_):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            audio_stream_future.set_result(rtc.AudioStream(track))

    def on_data(dp: rtc.DataPacket):
        nonlocal current_transcription
        print("Data received: ", dp)
        
        payload = json.loads(dp.data)
        
        if dp.topic == "lk-chat-topic":
            message = payload["message"]
            current_transcription = message
            print("USER MESSAGE")
            print(message)
            state.store_user_char(message)
            asyncio.create_task(handle_inference_task(chat_message=True))
        elif dp.topic == "character_card":
            # Handle character card data packet
            asyncio.create_task(handle_character_card(payload))
        elif dp.topic == "command":
            # Handle command data packet
            print("COMMAND")
            asyncio.create_task(handle_command(payload))
        else:
            print(f"Received data for unhandled topic: {dp.topic}")

    async def handle_character_card(payload):
        nonlocal intro_text, character_ready_event, model, canvas_model, canvas_interval, canvas_prompt
        # Implement handling of character card data packet
        print("Handling character card:", payload)
        intro_text, model, canvas_model, canvas_interval, canvas_prompt = state.update_character(payload["character"]) #TODO: fix this its hacky
        character_ready_event.set()

    async def handle_command(payload):
        # Implement handling of command data packet
        print("Handling command:", payload)
        if payload["data"].get("command") == "rgen":
            node_id = payload["data"].get("arg")
            await handel_rgen(node_id)
        elif payload["data"].get("command") == "alt":
            node_id = payload["data"].get("arg")
            await handel_alt(node_id)
    
    async def handel_rgen(node_id: str):
        nonlocal current_transcription
        current_transcription = state.roll_back_to_parent(node_id)
        print(current_transcription)
        asyncio.create_task(handle_inference_task(chat_message=True))
    
    async def handel_alt(node_id: str):
        state.change_active_node(node_id)
    
    async def handle_inference_task(force_text: str | None = None, chat_message: bool = False):
        nonlocal current_transcription, inference_task, model
        if inference_task:
            # Cancel in-flight inference
            inference_task.cancel()
            try:
                await inference_task
            except asyncio.CancelledError:
                pass
        # Start new inference
        inference_task = asyncio.create_task(start_new_inference(force_text=force_text, chat_message=chat_message, llm_model=model))

    for participant in job.room.participants.values():
        for track_pub in participant.tracks.values():
            # This track is not yet subscribed, when it is subscribed it will
            # call the on_track_subscribed callback
            if track_pub.track is None:
                continue
            audio_stream_future.set_result(rtc.AudioStream(track_pub.track))

    job.room.on("track_subscribed", on_track_subscribed)
    job.room.on("data_received", on_data)

    # Wait for user audio
    audio_stream = await audio_stream_future

    # Publish agent mic after waiting for user audio (simple way to avoid subscribing to self)
    await job.room.local_participant.publish_track(track, options)

    async def start_new_inference(force_text: str | None = None, chat_message: bool = False, llm_model: str = "mistralai/mixtral-8x7b-instruct:nitro"):
        nonlocal current_transcription

        state.agent_thinking = True
        job = InferenceJob(
            transcription=current_transcription,
            audio_source=source,
            chat_history=state.chat_history,
            force_text_response=force_text,
            llm_model=llm_model,
            voice_model=state.get_character().voice
        )

        try:
            agent_done_thinking = False
            agent_has_spoken = False
            comitted_agent = False

            def commit_agent_text_if_needed():
                nonlocal agent_has_spoken, agent_done_thinking, comitted_agent
                if agent_done_thinking and agent_has_spoken and not comitted_agent:
                    comitted_agent = True
                    state.commit_agent_response(job.current_response)

            async for e in job:
                # Allow cancellation
                if e.type == EventType.AGENT_RESPONSE:
                    if e.finished_generating:
                        state.agent_thinking = False
                        agent_done_thinking = True
                        commit_agent_text_if_needed()
                elif e.type == EventType.AGENT_SPEAKING:
                    state.agent_speaking = e.speaking
                    if e.speaking:
                        agent_has_spoken = True
                        # Only commit user text for real transcriptions
                        if not force_text and not chat_message:
                            state.commit_user_transcription(job.transcription)
                        commit_agent_text_if_needed()
                        current_transcription = ""
        except asyncio.CancelledError:
            await job.acancel()

    async def audio_stream_task():
        async for audio_frame_event in audio_stream:
            stt_stream.push_frame(audio_frame_event.frame)

    async def stt_stream_task():
        nonlocal current_transcription
        async for stt_event in stt_stream:
            # We eagerly try to run inference to keep the latency as low as possible.
            # If we get a new transcript, we update the working text, cancel in-flight inference,
            # and run new inference.
            if stt_event.type == agents.stt.SpeechEventType.FINAL_TRANSCRIPT:
                delta = stt_event.alternatives[0].text
                # Do nothing
                if delta == "":
                    continue
                current_transcription += " " + delta
                print(current_transcription)
                asyncio.create_task(handle_inference_task())
    
    async def create_new_canvas():
        nonlocal canvas_model, canvas_prompt
        print("CREATING CANVAS JOB")            
        job = cj.CanvasJob(
            chat_history=state.chat_history,
            llm_model=canvas_model,
            prompt=canvas_prompt
        )
        try:
            agent_done_thinking = False
            comitted_agent = False

            def commit_agent_text_if_needed():
                nonlocal agent_done_thinking, comitted_agent
                if agent_done_thinking and not comitted_agent:
                    comitted_agent = True
                    state.commit_canvas_response(job.current_response)

            async for e in job:
                # Allow cancellation
                if e.type == cj.EventType.AGENT_RESPONSE:
                    if e.finished_generating:
                        agent_done_thinking = True
                        commit_agent_text_if_needed()

            print("Finished canvas job")            
        except asyncio.CancelledError:
            await job.acancel()
    
    async def canvas_task():
        nonlocal canvas_interval, c_task
        while True:
            try:
                print("Creating a new canvas")
                c_task = asyncio.create_task(create_new_canvas())
                await asyncio.sleep(canvas_interval)
            except asyncio.CancelledError:
                # if c_task is not None:
                    # await c_task.cancel()
                break
            except Exception as e:
                break

    try:
        await character_ready_event.wait()
        character_ready_event = asyncio.create_task(start_new_inference(force_text=intro_text, llm_model=model))
        #TODO: we need to eventally kill these tasks (i don't think they ever stop)
        async with asyncio.TaskGroup() as tg:
            tg.create_task(audio_stream_task())
            tg.create_task(stt_stream_task())
        # async with asyncio.TaskGroup() as cv_tg:
            # tg.create_task(canvas_task())
    except BaseExceptionGroup as e:
        for exc in e.exceptions:
            print("Exception: ", exc)
    except Exception as e:
        print("Exception: ", e)

active_sessions = set()
async def request_fnc(req: JobRequest) -> None:
    room_name = req.room.name
    if room_name not in active_sessions:
            active_sessions.add(room_name)
            await req.accept(entrypoint, auto_subscribe=agents.AutoSubscribe.AUDIO_ONLY)
    else:
        print(f"Session {room_name} already has an active agent.")

if __name__ == "__main__":
    cli.run_app(WorkerOptions(request_fnc=request_fnc))
