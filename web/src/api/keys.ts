// API key 存 localStorage:consumer key 用于 search/task/feedback,provider key 用于注册。

const CONSUMER_KEY = "agentnet.consumer_key";
const PROVIDER_KEY = "agentnet.provider_key";

export function getConsumerKey(): string {
  return localStorage.getItem(CONSUMER_KEY) ?? "";
}

export function getProviderKey(): string {
  return localStorage.getItem(PROVIDER_KEY) ?? "";
}

export function saveKeys(consumerKey: string, providerKey: string): void {
  localStorage.setItem(CONSUMER_KEY, consumerKey.trim());
  localStorage.setItem(PROVIDER_KEY, providerKey.trim());
}
