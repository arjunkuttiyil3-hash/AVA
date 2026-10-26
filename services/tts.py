import edge_tts
import asyncio
import os


# ============================================================
# AVA TEXT-TO-SPEECH
# Indian Female Voice
# ============================================================

VOICE = "en-IN-NeerjaNeural"


async def generate_audio_async(
    text,
    output_file,
    rate="+0%",
    volume="+0%",
    pitch="+0Hz"
):
    """
    Generate speech using Microsoft's Edge TTS service.

    Voice:
        Indian Female - Neerja

    Args:
        text: Text that AVA should speak
        output_file: Path where MP3 should be saved
        rate: Speech speed
        volume: Speech volume
        pitch: Voice pitch
    """

    # Make sure output directory exists
    output_directory = os.path.dirname(output_file)

    if output_directory:
        os.makedirs(output_directory, exist_ok=True)

    # Create TTS communicator
    communicate = edge_tts.Communicate(
        text=text,
        voice=VOICE,
        rate=rate,
        volume=volume,
        pitch=pitch
    )

    # Generate MP3
    await communicate.save(output_file)


def generate_audio(
    text,
    output_file,
    rate="+0%",
    volume="+0%",
    pitch="+0Hz"
):
    """
    Synchronous wrapper for AVA TTS.
    """

    asyncio.run(
        generate_audio_async(
            text=text,
            output_file=output_file,
            rate=rate,
            volume=volume,
            pitch=pitch
        )
    )

    return output_file