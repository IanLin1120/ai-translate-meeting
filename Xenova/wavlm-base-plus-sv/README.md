---
base_model: microsoft/wavlm-base-plus-sv
library_name: transformers.js
---

https://huggingface.co/microsoft/wavlm-base-plus-sv with ONNX weights to be compatible with Transformers.js.

## Usage (Transformers.js)

If you haven't already, you can install the [Transformers.js](https://huggingface.co/docs/transformers.js) JavaScript library from [NPM](https://www.npmjs.com/package/@xenova/transformers) using:
```bash
npm i @xenova/transformers
```

**Example:** Speaker verification w/ `Xenova/wavlm-base-plus-sv`.

```js
import { AutoProcessor, AutoModel, read_audio, cos_sim } from '@xenova/transformers';

// Load processor and model
const processor = await AutoProcessor.from_pretrained('Xenova/wavlm-base-plus-sv');
const model = await AutoModel.from_pretrained('Xenova/wavlm-base-plus-sv');

// Helper function to compute speaker embedding from audio URL
async function compute_embedding(url) {
    const audio = await read_audio(url, 16000);
    const inputs = await processor(audio);
    const { embeddings } = await model(inputs);
    return embeddings.data;
}

// Generate speaker embeddings
const BASE_URL = 'https://huggingface.co/datasets/Xenova/transformers.js-docs/resolve/main/sv_speaker';
const speaker_1_1 = await compute_embedding(`${BASE_URL}-1_1.wav`);
const speaker_1_2 = await compute_embedding(`${BASE_URL}-1_2.wav`);
const speaker_2_1 = await compute_embedding(`${BASE_URL}-2_1.wav`);
const speaker_2_2 = await compute_embedding(`${BASE_URL}-2_2.wav`);

// Compute similarity scores
console.log(cos_sim(speaker_1_1, speaker_1_2)); // 0.959439158881247 (Both are speaker 1)
console.log(cos_sim(speaker_1_2, speaker_2_1)); // 0.618130172602329 (Different speakers)
console.log(cos_sim(speaker_2_1, speaker_2_2)); // 0.962999814169370 (Both are speaker 2)
```

---

Note: Having a separate repo for ONNX weights is intended to be a temporary solution until WebML gains more traction. If you would like to make your models web-ready, we recommend converting to ONNX using [🤗 Optimum](https://huggingface.co/docs/optimum/index) and structuring your repo like this one (with ONNX weights located in a subfolder named `onnx`).