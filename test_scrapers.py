import asyncio
import os
from ieee_scraper import scraper_instance as ieee
from sd_scraper import scraper_instance as sd

async def run_tests():
    output_dir = os.path.join(os.getcwd(), "scratch_test_downloads")
    os.makedirs(output_dir, exist_ok=True)
    
    print("--- 启动 IEEE 实网穿透测试 ---")
    ieee_res = await ieee.download_paper("https://ieeexplore.ieee.org/document/11321545/", output_dir)
    print("IEEE 下载结果:", ieee_res)
    if os.path.exists(ieee_res) and os.path.isfile(ieee_res):
        print(f"IEEE 验证成功! 文件大小: {os.path.getsize(ieee_res)} 字节")
    else:
        print("IEEE 验证失败!")
        
    print("\n--- 启动 ScienceDirect 实网穿透测试 ---")
    sd_res = await sd.download_paper("https://www.sciencedirect.com/science/article/pii/S2452414X26000191", output_dir)
    print("ScienceDirect 下载结果:", sd_res)
    if os.path.exists(sd_res) and os.path.isfile(sd_res):
        print(f"ScienceDirect 验证成功! 文件大小: {os.path.getsize(sd_res)} 字节")
    else:
        print("ScienceDirect 验证失败!")

    await ieee.close()
    await sd.close()

if __name__ == "__main__":
    asyncio.run(run_tests())
