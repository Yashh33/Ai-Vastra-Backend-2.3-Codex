// Same list as the backend (config.IMAGE_MODELS).
export const IMAGE_MODEL_OPTIONS = [
  { id: "gemini-nano-banana-2.1", label: "Nano Banana 2.1", short: "NB 2.1" },
  { id: "gemini-3.1-flash-image-preview", label: "Nano Banana 2", short: "NB 2" },
];

function findModel(modelId: string) {
  return IMAGE_MODEL_OPTIONS.find((option) => option.id === modelId);
}

/** "Nano Banana 2"; the raw id if the model is not in the list. */
export function imageModelLabel(modelId: string): string {
  return findModel(modelId)?.label ?? modelId;
}

/** "NB 2"; the raw id if the model is not in the list. */
export function imageModelShortLabel(modelId: string): string {
  return findModel(modelId)?.short ?? modelId;
}

/** "Nano Banana 2 (gemini-3.1-flash-image-preview)"; just the raw id if the model is not in the list. */
export function imageModelFullLabel(modelId: string): string {
  const model = findModel(modelId);
  return model ? `${model.label} (${modelId})` : modelId;
}
