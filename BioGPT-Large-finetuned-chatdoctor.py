import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig


model_id = "Narrativaai/BioGPT-Large-finetuned-chatdoctor"

tokenizer = AutoTokenizer.from_pretrained("microsoft/BioGPT-Large")

model = AutoModelForCausalLM.from_pretrained(model_id)

def answer_question(
        prompt,
        temperature=0.1,
        top_p=0.75,
        top_k=40,
        num_beams=2,
        **kwargs,
):
    inputs = tokenizer(prompt, return_tensors="pt")
    input_ids = inputs["input_ids"].cuda()
    attention_mask = inputs["attention_mask"].cuda()
    generation_config = GenerationConfig(
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        num_beams=num_beams,
        **kwargs,
    )
    with torch.no_grad():
        generation_output = model.generate(
            input_ids=input_ids,
            attention_mask=attention_mask,
            generation_config=generation_config,
            return_dict_in_generate=True,
            output_scores=True,
            max_new_tokens=512,
            eos_token_id=tokenizer.eos_token_id

        )
    s = generation_output.sequences[0]
    output = tokenizer.decode(s, skip_special_tokens=True)
    return output.split(" Response:")[1]

example_prompt = """
Below is an instruction that describes a task, paired with an input that provides further context.Write a response that appropriately completes the request.

### Instruction:
If you are a doctor, please answer the medical questions based on the patient's description.

### Input:
Hi i have sore lumps under the skin on my legs. they started on my left ankle and are approx 1 - 2cm diameter and are spreading up onto my thies. I am eating panadol night and anti allergy pills (Atarax). I have had this for about two weeks now. Please advise.

### Response:
"""

print(answer_question(example_prompt))
