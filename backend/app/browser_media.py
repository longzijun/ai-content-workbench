"""Uploads only trusted version assets through observed official UI controls."""
from pathlib import Path
import re


def assets(work):
    return work.get('_image_assets', [])


def _paths(work):
    files = assets(work)
    if not files or any(not Path(a['path']).is_file() for a in files):
        raise ValueError('当前固定版本缺少真实图片文件')
    return [str(Path(a['path'])) for a in files]


def _loaded(page, selector, count, timeout=30000):
    page.wait_for_function('''expected => {
      const nodes=[...document.querySelectorAll(expected.selector)];
      return nodes.length===expected.count&&nodes.every(e=>e.complete&&e.naturalWidth>0&&e.naturalHeight>0);
    }''', arg={'selector':selector,'count':count}, timeout=timeout)


def article_image_selector(platform):
    return '.ProseMirror[contenteditable="true"] img.rich_pages.wxw-img' if platform=='wechat' else '.ProseMirror[contenteditable="true"] .pgc-img-wrapper .img-loading-container img'


def _wechat_cover_loaded(page, timeout=30000):
    page.wait_for_function('''() => {
      const preview=document.querySelector('.setting-group__cover_primary .js_cover_preview_new');
      const loader=document.querySelector('.setting-group__cover_primary .js_cover_loading');
      if(!preview||!preview.getClientRects().length||getComputedStyle(preview).display==='none')return false;
      if(loader&&getComputedStyle(loader).visibility!=='hidden')return false;
      const bg=getComputedStyle(preview).backgroundImage;
      const match=bg.match(/^url\\(["']?(https:\\/\\/[^"')]+)["']?\\)$/);
      if(!match)return false;
      const value=new URL(match[1]);
      if(!['mmbiz.qpic.cn','mmbiz.qlogo.cn','wx.qlogo.cn'].includes(value.hostname)||value.username||value.password)return false;
      return new Promise(resolve=>{const image=new Image();image.onload=()=>resolve(image.naturalWidth>0&&image.naturalHeight>0);image.onerror=()=>resolve(false);image.src=match[1];});
    }''',timeout=timeout)


def choose_wechat_cover(editor, work):
    """Pick the first trusted body asset via the observed native cover crop UI."""
    trigger=editor.locator('.setting-group__cover_primary .js_share_type_none_image').filter(visible=True)
    if trigger.count()!=1:
        raise RuntimeError('公众号空白封面入口未唯一识别，未替换其他封面')
    trigger.click(timeout=10000)
    entry=editor.locator('a.js_selectCoverFromContent').filter(visible=True)
    entry.first.wait_for(timeout=10000)
    if entry.count()!=1:
        raise RuntimeError('公众号从正文选择封面入口未唯一识别')
    entry.click(timeout=10000)
    items=editor.locator('.appmsg_conent_img_container .appmsg_content_img_item').filter(visible=True)
    items.first.wait_for(timeout=15000)
    if items.count()!=len(assets(work)):
        raise RuntimeError('正文封面候选数量与固定版本实际图片不一致')
    items.first.click(timeout=10000)
    step=editor.get_by_role('button',name='下一步',exact=True).filter(visible=True)
    if step.count()!=1:
        raise RuntimeError('公众号封面下一步控件未唯一识别')
    step.click(timeout=10000)
    confirm=editor.get_by_role('button',name='确认',exact=True).filter(visible=True)
    confirm.first.wait_for(timeout=30000)
    if confirm.count()!=1:
        raise RuntimeError('公众号封面裁剪确认控件未唯一识别')
    confirm.click(timeout=10000)
    try:
        _wechat_cover_loaded(editor, timeout=20000)
    except Exception:
        # 封面预览回读未确认（可能是加载慢或域名判断未命中）：不阻断正文保存，
        # 由 verify_media 在保存后如实标记 cover_verified，用户可人工核对或补封面。
        pass


def _place_caret_at_end(body_field):
    """把光标放到正文末尾，供随后插入图片。

    **不要用 `body_field.click()`**。Playwright 的 click 点的是元素中心，
    ProseMirror 会把光标定位到点击处；之后的 `Control+End` 由编辑器自己
    管理选区，在长正文里不一定生效。2026-10-22 的头条实测就是这样：
    三张图插在了第三段的位置，把那一段覆盖掉了 —— 平台草稿里留下了
    缺段正文，而写入阶段一切「成功」。

    这里直接用 DOM Range 把选区折叠到末尾，不依赖任何点击坐标。
    """
    body_field.focus(timeout=10000)
    body_field.evaluate("""el => {
        el.focus();
        const range = document.createRange();
        range.selectNodeContents(el);
        range.collapse(false);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
    }""")


def upload_article(editor, work, body_field):
    if not assets(work):
        return
    platform = work['platform']
    _place_caret_at_end(body_field)
    body_field.press('Enter')
    if platform == 'wechat':
        # 人工路径：本地上传入口只在菜单展开后才绑定上传流程，直接 set_input_files
        # 隐藏 input 不生效（实测 upload_material 不会发出）。必须点击工具栏图片按钮
        # 展开菜单，点击「本地上传」菜单项，再用文件选择器注入真实文件。
        toolbar = editor.locator('li.tpl_item.tpl_item_dropdown.jsInsertIcon.img').filter(visible=True)
        if toolbar.count() != 1:
            raise RuntimeError('公众号工具栏图片按钮未唯一识别')
        toolbar.click(timeout=10000)
        editor.wait_for_timeout(1200)
        local = editor.locator('li.tpl_dropdown_menu_item:has-text("本地上传")')
        local.first.wait_for(state='visible', timeout=10000)
        if local.count() != 1:
            raise RuntimeError('公众号本地上传菜单项未唯一识别')
        with editor.expect_file_chooser(timeout=20000) as chooser_info:
            local.first.click(timeout=10000)
        chooser_info.value.set_files(_paths(work), timeout=60000)
    elif platform == 'toutiao':
        drawer = editor.locator('.ai-assistant-drawer').filter(visible=True)
        close = drawer.locator('.close-btn').filter(visible=True)
        if close.count() == 1:
            close.click(timeout=5000)
        tool = editor.locator('.syl-toolbar-tool.image.static button').filter(visible=True)
        if tool.count() != 1:
            raise RuntimeError('头条正文图片入口未唯一识别')
        tool.click(timeout=10000)
        upload = editor.locator('.btn-upload-handle.upload-handler input[type="file"][accept="image/*"]')
        upload.wait_for(state='attached', timeout=15000)
        if upload.count() != 1:
            raise RuntimeError('头条本地图片上传入口未唯一识别')
        upload.set_input_files(_paths(work),timeout=30000)
        # The image dialog can require an explicit insertion confirmation.
        editor.wait_for_timeout(1200)
        if editor.locator(article_image_selector(platform)).count() != len(assets(work)):
            confirm = editor.get_by_role('button',name=re.compile(r'^(?:确定|确认|插入|完成)(?:图片)?$')).filter(visible=True)
            confirm.first.wait_for(state='visible', timeout=20000)
            if confirm.count() != 1:
                raise RuntimeError('头条图片插入确认按钮未唯一识别')
            confirm.click(timeout=10000)
    else:
        raise ValueError('此上传函数仅处理文章正文')
    _loaded(editor,article_image_selector(platform),len(assets(work)))
    if platform=='wechat':
        try:
            choose_wechat_cover(editor,work)
        except Exception:
            # 封面设置未完成（控件变化或预览超时）不应中断正文保存；
            # verify_media 会在保存后如实报告 cover_verified=False，供人工核对。
            pass


def upload_note(editor, work):
    upload = editor.locator('input[type="file"][accept=".jpg,.jpeg,.png,.webp"][multiple]')
    if upload.count() != 1:
        raise RuntimeError('小红书空白图文上传入口未唯一识别')
    upload.set_input_files(_paths(work),timeout=30000)
    editor.get_by_placeholder('填写标题会有更多赞哦',exact=True).filter(visible=True).wait_for(timeout=60000)


def verify_media(page, work):
    expected = len(assets(work))
    if not expected:
        # 2026-10-20 决策：配图只由已注册源生图资产驱动；无资产即无图交付。
        return {'status':'not_required','expected_count':0,'verified_count':0,'cover_verified':False}
    editor = getattr(page,'_workbench_verified_editor',page)
    platform = work['platform']
    selector = '.image-preview .swiper-wrapper img.preivew-image' if platform=='xiaohongshu' else article_image_selector(platform)
    try:
        _loaded(editor,selector,expected,timeout=15000)
        count = editor.locator(selector).count()
    except Exception:
        count = 0
    if platform=='xiaohongshu':
        cover = count==expected and editor.locator('.img-container img.img.preview').count()==expected
    elif platform=='wechat':
        try:
            _wechat_cover_loaded(editor,timeout=15000)
            cover = True
        except Exception:
            cover = False
    else:
        cover = editor.locator('.article-cover-img-wrap img').filter(visible=True).count()==1
    return {'status':'verified' if count==expected and cover else 'unverified','expected_count':expected,'verified_count':count,'cover_verified':cover,'error':None if count==expected and cover else '正文图片或封面回读尚未完整确认'}
