# -*- coding: gbk -*-
# 这是一个用 GBK 编码保存的历史文件（中文注释）
def legacy_handler(payload):
    # 中文注释：旧系统上传的数据可能是 GBK 编码
    try:
        return payload.decode('gbk')
    except:
        return ''