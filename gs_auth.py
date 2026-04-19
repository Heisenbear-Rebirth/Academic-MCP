import os
import asyncio
from playwright.async_api import async_playwright

async def run_auth():
    print("启动 Google Scholar 认证会话...")
    print("如有验证码请手动完成（红绿灯图形等），完成后不要关闭浏览器，回到这里按 Enter 继续以保存 Cookie！")
    
    profile_dir = os.path.join(os.getcwd(), ".gs_profile")
    for lock_name in ["lockfile", "SingletonLock"]:
        lfile = os.path.join(profile_dir, lock_name)
        if os.path.exists(lfile):
            try: os.remove(lfile)
            except: pass

    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(
        headless=False,
        user_data_dir=profile_dir,
        persistent_context=True,
        humanize=True,
        geoip=True
    ) as context:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://scholar.google.com/scholar?hl=en&q=test")
        
        input("当您在弹出窗口中完成任何可能的验证码后，按 Enter 键以保存会话...")
        
        await context.close()
        print("会话已保存到 .gs_profile 中！MCP 即可免疫验证码执行检索！")

if __name__ == "__main__":
    asyncio.run(run_auth())
