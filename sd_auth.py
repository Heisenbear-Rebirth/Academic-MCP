import asyncio
from playwright.async_api import async_playwright
import os

async def main():
    print("=== ScienceDirect / Elsevier 破盾初始化工具 ===")
    print("此工具将以带窗口模式启动 Chrome，并为您建立完全受信的上下文（Cookie）。")
    print("如果浏览器弹窗后要求您进行人机验证（点击“我不是机器人”），请尽快点取！")
    print("等待 40 秒供您操作后，程序会自动保存凭证并退出。这应该只需做一次。")
    print("-" * 50)
    
    profile_dir = os.path.abspath(".sd_profile")
    
    async with async_playwright() as p:
        # Launch totally unhidden headful mode
        context = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--window-position=50,50"],
            viewport={"width": 1280, "height": 720}
        )
        
        page = context.pages[0] if context.pages else await context.new_page()
        
        print("\n\n>>> 正在前往 ScienceDirect 核心验证页面...请随时准备点击 <<<")
        await page.goto("https://www.sciencedirect.com/search?qs=auth+init", wait_until="domcontentloaded")
        
        # Give the user ample time
        for i in range(40):
            print(f"[{40-i}秒剩余] 等待通过 DataDome...", end='\r')
            await asyncio.sleep(1)
            
        title = await page.title()
        html = await page.content()
        if "Are you a robot?" in html or "请稍候" in title:
            print("\n[警告] 您似乎还没来得及通过验证，或者验证失败了。请重新运行本脚本！")
        else:
            print(f"\n[成功] 当前页面标题为: {title}")
            print("Cookie 已固化进入 .sd_profile 文件夹！您可以关闭本黑框，并尽情使用后台静默 MCP 了！")
        
        await context.close()

if __name__ == "__main__":
    asyncio.run(main())
