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
    participant = (item["participant"] or "").strip()
    title_words = set(re.findall(r"\w+", title.casefold()))
    participant_words = set(re.findall(r"\w+", participant.casefold()))

    if not participant:
        participant_text = "Not provided"
    elif participant_words.issubset(title_words):
        participant_text = "Already included in the title"
    else:
        participant_text = participant

    return f"Title: {title}\nParticipant: {participant_text}"


def generate_script(event_name, previous, current, next_event, style="Energetic"):

    previous_text = _event_details(previous) if previous else "Not available"
    current_text = _event_details(current)
    next_text = _event_details(next_event) if next_event else "Not available"

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

    prompt = f"""
You are AVA, an AI stage anchor hosting a college event.

Your job is to write a natural spoken announcement for the
CURRENT segment of the event.

Event:
{event_name}

Previous Event:
{previous_text}

Current Event:
{current_text}

Next Event:
{next_text}

ANCHORING STYLE:
{style_instruction}

CONTENT AND DELIVERY REQUIREMENTS:

- Write 2 or 3 short sentences, about 25 to 45 words total.
- Introduce the current title once. Mention the participant only if not already included in the title.
- If a previous event is listed, use at most one brief transition to it.
- Mention the next event only when one is listed; otherwise end after introducing the current event.
- Use only the supplied details. Do not invent atmosphere, audience reactions, praise, achievements, genre, or event facts.
- Avoid repeating names, titles, ideas, and stock phrases such as "get ready" or "wild night".
- Make the chosen style affect wording and rhythm only, never the facts.
- Use natural, concise spoken English.
- Do not make every announcement sound identical.
- Output ONLY the announcement.
- Do not include headings, quotation marks, markdown or stage directions.

Generate the announcement now.
"""

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