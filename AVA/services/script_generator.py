import re

from transformers import AutoModelForCausalLM, AutoTokenizer
import torch


MODEL_NAME = "Qwen/Qwen2.5-3B-Instruct"

tokenizer = None
model = None
model_device = None


def _load_model():
    global tokenizer, model, model_device

    if tokenizer is None or model is None:
        print("Loading AVA AI model...")
        loaded_tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
        model_device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        model_dtype = torch.float16 if model_device.type == "cuda" else torch.float32

        try:
            loaded_model = AutoModelForCausalLM.from_pretrained(
                MODEL_NAME,
                torch_dtype=model_dtype,
                low_cpu_mem_usage=True
            ).to(model_device)
        except torch.cuda.OutOfMemoryError:
            if model_device.type != "cuda":
                raise

            print("CUDA memory is insufficient; loading AVA AI model on CPU...")
            torch.cuda.empty_cache()
            model_device = torch.device("cpu")
            loaded_model = AutoModelForCausalLM.from_pretrained(
                MODEL_NAME,
                torch_dtype=torch.float32,
                low_cpu_mem_usage=True
            )

        tokenizer = loaded_tokenizer
        model = loaded_model
        print("AVA AI model loaded!")

    return tokenizer, model


def _event_details(item):
    title = item["title"]
<<<<<<< HEAD
    participant = (item["participant"] if "participant" in item.keys() else "")
    participant = (participant or "").strip()
    description = (item["description"] if "description" in item.keys() else "")
    description = (description or "").strip()
=======
    participant = (item["participant"] or "").strip()
>>>>>>> 93044976bf212e81ece22241ed8689e82df156e2
    title_words = set(re.findall(r"\w+", title.casefold()))
    participant_words = set(re.findall(r"\w+", participant.casefold()))

    if not participant:
        participant_text = "Not provided"
    elif participant_words.issubset(title_words):
        participant_text = "Already included in the title"
    else:
        participant_text = participant

<<<<<<< HEAD
    details = f"Title: {title}\nParticipant: {participant_text}"
    if description:
        details += f"\nDescription: {description}"

    return details


def _build_script_prompt(event_name, event_description, previous, current, next_event, style):
    previous_text = _event_details(previous) if previous else "Not available"
    current_text = _event_details(current)
    next_text = _event_details(next_event) if next_event else "Not available"
    current_has_speaker = bool(
        current is not None and "participant" in current.keys() and current["participant"]
    )
    event_description = event_description or ""
=======
    return f"Title: {title}\nParticipant: {participant_text}"


def generate_script(event_name, previous, current, next_event, style="Energetic"):

    previous_text = _event_details(previous) if previous else "Not available"
    current_text = _event_details(current)
    next_text = _event_details(next_event) if next_event else "Not available"
>>>>>>> 93044976bf212e81ece22241ed8689e82df156e2

    styles = {

        "Energetic": """
    Use an upbeat, lively delivery with clear, confident phrasing.
    Do not exaggerate or make claims about the event or audience.
""",

        "Professional": """
Use a polished, formal and professional event-host style.
Keep the language elegant, confident and well structured.
Avoid slang and excessive excitement.
""",

        "Youthful": """
Use a modern, youthful college-event hosting style.
Make it conversational, lively and relatable while remaining professional.
The script should sound natural when spoken by a young college anchor.
""",

        "Funny": """
Use a light-hearted and slightly humorous hosting style.
Keep the tone gently playful, but do not add jokes or details unsupported by the event information.
""",

        "Elegant": """
Use a sophisticated and graceful stage-host style.
Keep the language smooth, confident and engaging.
Use refined transitions and avoid excessive dramatic expressions.
""",

        "Formal": """
Use a formal ceremonial event-host style.
Keep the announcement respectful, structured and concise.
Avoid casual expressions and humor.
"""
    }

    style_instruction = styles.get(
        style,
        styles["Energetic"]
    )

<<<<<<< HEAD
    speaker_instruction = ""
    if not current_has_speaker:
        speaker_instruction = (
            "No speaker is provided for the current event. Introduce the program "
            "using the event description and briefly explain what the program includes."
        )

    prompt = f"""
You are AVA, the virtual anchor for this college event.

You must deliver the introduction and announcement yourself.
Do not assign the introduction to another person, host, speaker, or participant.
=======
    prompt = f"""
You are AVA, an AI stage anchor hosting a college event.
>>>>>>> 93044976bf212e81ece22241ed8689e82df156e2

Your job is to write a natural spoken announcement for the
CURRENT segment of the event.

Event:
{event_name}

<<<<<<< HEAD
Program Description:
{event_description or 'Not provided'}

=======
>>>>>>> 93044976bf212e81ece22241ed8689e82df156e2
Previous Event:
{previous_text}

Current Event:
{current_text}

Next Event:
{next_text}

ANCHORING STYLE:
{style_instruction}

CONTENT AND DELIVERY REQUIREMENTS:

<<<<<<< HEAD
- Write exactly 2 or 3 short sentences, with about 25 to 45 words total.
- Introduce the current title once. Mention the participant only if not already included in the title.
- Include the current description when one is provided, using it as factual context for the announcement.
- {speaker_instruction}
- If a previous event is listed, use at most one brief transition to it.
- Mention the next event only when one is listed; otherwise end after introducing the current event.
- Use only factual, grammatically correct sentences based on the supplied details.
- Add warm, encouraging audience engagement through genuine appreciation, supportive phrasing, or a brief invitation to participate, without inventing reactions or claims.
- Do not invent atmosphere, audience reactions, praise, achievements, genre, or event facts.
- Do not use the phrase "welcome back" or other generic returns.
- Avoid repeating names, titles, ideas, and stock phrases such as "get ready" or "wild night".
- Make the chosen style affect wording and rhythm only, never the facts.
- Use natural, concise spoken English with correct punctuation and clear subject-verb agreement.
- Keep the engagement sentence simple and brief so the event information remains clear.
=======
- Write 2 or 3 short sentences, about 25 to 45 words total.
- Introduce the current title once. Mention the participant only if not already included in the title.
- If a previous event is listed, use at most one brief transition to it.
- Mention the next event only when one is listed; otherwise end after introducing the current event.
- Use only the supplied details. Do not invent atmosphere, audience reactions, praise, achievements, genre, or event facts.
- Avoid repeating names, titles, ideas, and stock phrases such as "get ready" or "wild night".
- Make the chosen style affect wording and rhythm only, never the facts.
- Use natural, concise spoken English.
>>>>>>> 93044976bf212e81ece22241ed8689e82df156e2
- Do not make every announcement sound identical.
- Output ONLY the announcement.
- Do not include headings, quotation marks, markdown or stage directions.

Generate the announcement now.
"""

<<<<<<< HEAD
    return prompt


def generate_script(event_name, event_description, previous, current, next_event, style="Energetic"):
    prompt = _build_script_prompt(
        event_name,
        event_description,
        previous,
        current,
        next_event,
        style
    )

=======
>>>>>>> 93044976bf212e81ece22241ed8689e82df156e2
    messages = [
        {
            "role": "system",
            "content": (
                "You are AVA, a professional AI stage anchor "
                "specialized in college events."
            )
        },
        {
            "role": "user",
            "content": prompt
        }
    ]

    tokenizer, model = _load_model()

    text = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True
    )

    inputs = tokenizer(
        text,
        return_tensors="pt"
    ).to(model_device)

    with torch.no_grad():

        output = model.generate(
            **inputs,
            max_new_tokens=100,
            temperature=0.5,
            top_p=0.85,
            repetition_penalty=1.15,
            no_repeat_ngram_size=3,
            do_sample=True
        )

    generated = output[0][inputs["input_ids"].shape[1]:]

    script = tokenizer.decode(
        generated,
        skip_special_tokens=True
    )

    return script.strip()