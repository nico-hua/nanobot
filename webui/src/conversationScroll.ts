const AUTO_SCROLL_THRESHOLD_PX = 48;

type ScrollMetrics = {
  scrollHeight: number;
  scrollTop: number;
  clientHeight: number;
};

/** Whether a conversation viewport is close enough to keep following updates. */
export function isNearConversationBottom({
  scrollHeight,
  scrollTop,
  clientHeight,
}: ScrollMetrics): boolean {
  return scrollHeight - scrollTop - clientHeight <= AUTO_SCROLL_THRESHOLD_PX;
}
