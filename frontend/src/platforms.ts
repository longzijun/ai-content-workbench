import type { Platform } from './types';

export const PLATFORM_IDS: Platform[] = ['wechat', 'toutiao', 'xiaohongshu', 'weixin_channels', 'kuaishou', 'douyin'];
export const PLATFORM_NAMES: Record<Platform, string> = { wechat: '公众号', toutiao: '今日头条', xiaohongshu: '小红书', weixin_channels: '视频号', kuaishou: '快手', douyin: '抖音' };
export const VIDEO_PLATFORMS: Platform[] = ['weixin_channels', 'kuaishou', 'douyin'];
export const isVideoPlatform = (platform: Platform) => VIDEO_PLATFORMS.includes(platform);
