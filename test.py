import openai

client = openai.OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="changeme_local_only"  # Не используется, но обязателен
)

stream = client.chat.completions.create(
    model="gemini-2.5-pro",
    messages=[
        {
            "role": "user",
            "content": "Расскажи мне анекдот",
        },
    ],
    stream=True,
        # extra_body={
        #   'extra_body': {
        #     "google": {
        #       "thinking_config": {
        #         "thinking_budget": 32768, # 128 to 32768
        #         "include_thoughts": True
        #       }
        #     }
        #   }
        # }
)

for chunk in stream:
    content = chunk.choices[0].delta.content
    if content:
        print(content, end="", flush=True)

print()
