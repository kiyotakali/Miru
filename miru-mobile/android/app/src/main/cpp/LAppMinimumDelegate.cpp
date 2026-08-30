/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */

#include "LAppMinimumDelegate.hpp"
#include <iostream>
#include <GLES2/gl2.h>
#include "LAppMinimumView.hpp"
#include "LAppPal.hpp"
#include "LAppDefine.hpp"
#include "LAppMinimumLive2DManager.hpp"
#include "LAppTextureManager.hpp"
#include "JniBridgeC.hpp"

using namespace Csm;
using namespace std;
using namespace LAppDefine;

namespace {
    LAppMinimumDelegate* s_instance = nullptr;
}

LAppMinimumDelegate* LAppMinimumDelegate::GetInstance()
{
    if (!s_instance)
    {
        s_instance = new LAppMinimumDelegate();
    }

    return s_instance;
}

void LAppMinimumDelegate::ReleaseInstance()
{
    if (s_instance)
    {
        delete s_instance;
    }

    s_instance = nullptr;
}


void LAppMinimumDelegate::OnStart()
{
    _textureManager = new LAppTextureManager();
    _view = new LAppMinimumView();
    LAppPal::UpdateTime();
}

void LAppMinimumDelegate::OnPause()
{

}

void LAppMinimumDelegate::OnStop()
{
    _effects.Cleanup();

    if (_view)
    {
        delete _view;
        _view = nullptr;
    }
    if (_textureManager)
    {
        delete _textureManager;
        _textureManager = nullptr;
    }

    // リソースを解放
    LAppMinimumLive2DManager::ReleaseInstance();

    CubismFramework::Dispose();
}

void LAppMinimumDelegate::OnDestroy()
{
    ReleaseInstance();
}

void LAppMinimumDelegate::Run()
{
    LAppPal::UpdateTime();
    float dt = LAppPal::GetDeltaTime();

    glClear(GL_DEPTH_BUFFER_BIT);
    glClearDepthf(1.0f);

    _effects.Update(dt);

    // Pre-model layers (behind character)
    _effects.DrawBackground();
    _effects.DrawStars();
    _effects.DrawLightRays();
    _effects.DrawGlow();

    // Restore blend state for Live2D renderer
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);

    if (_view)
    {
        _view->Render();
    }

    // Post-model layers (in front of character)
    _effects.DrawParticles();
    _effects.DrawDustMotes();
    _effects.DrawVignette();
    _effects.DrawBubble();

    if(!_isActive)
    {
        JniBridgeC::MoveTaskToBack();
    }
}

void LAppMinimumDelegate::OnSurfaceCreate()
{
    //テクスチャサンプリング設定
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);

    //透過設定
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);

    //Initialize cubism
    CubismFramework::Initialize();
}

void LAppMinimumDelegate::OnSurfaceChanged(float width, float height)
{
    glViewport(0, 0, width, height);
    _width = width;
    _height = height;

    //AppViewの初期化
    _view->Initialize();
    _view->InitializeSprite();

    // Initialize wallpaper visual effects
    _effects.Init(static_cast<int>(width), static_cast<int>(height));

    _isActive = true;
}

LAppMinimumDelegate::LAppMinimumDelegate():
    _cubismOption(),
    _captured(false),
    _mouseX(0.0f),
    _mouseY(0.0f),
    _isActive(true),
    _textureManager(nullptr),
    _view(nullptr)
{
    // setup view
    int width,height;
    glViewport(0, 0, width, height);
    _width = width;
    _height = height;

    // Setup Cubism
    _cubismOption.LogFunction = LAppPal::PrintMessageLn;
    _cubismOption.LoggingLevel = LAppDefine::CubismLoggingLevel;
    CubismFramework::CleanUp();
    CubismFramework::StartUp(&_cubismAllocator, &_cubismOption);
}

LAppMinimumDelegate::~LAppMinimumDelegate()
{
}

void LAppMinimumDelegate::OnTouchBegan(double x, double y)
{
    _mouseX = static_cast<float>(x);
    _mouseY = static_cast<float>(y);

    if (_view)
    {
        _captured = true;
        _view->OnTouchesBegan(_mouseX, _mouseY);
    }
}

void LAppMinimumDelegate::OnTouchEnded(double x, double y)
{
    _mouseX = static_cast<float>(x);
    _mouseY = static_cast<float>(y);

    if (_view)
    {
        _captured = false;
        _view->OnTouchesEnded(_mouseX, _mouseY);
    }
}

void LAppMinimumDelegate::OnTouchMoved(double x, double y)
{
    _mouseX = static_cast<float>(x);
    _mouseY = static_cast<float>(y);

    if (_captured && _view)
    {
        _view->OnTouchesMoved(_mouseX, _mouseY);
    }
}
