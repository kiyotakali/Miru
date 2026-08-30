cmake_minimum_required(VERSION 3.18)
project(MiruLive2D)

set(APP_NAME MiruLive2D)
set(CORE_PATH ${CMAKE_CURRENT_SOURCE_DIR}/CubismCore)
set(FRAMEWORK_PATH ${CMAKE_CURRENT_SOURCE_DIR}/Framework)
set(STB_PATH ${CMAKE_CURRENT_SOURCE_DIR}/thirdParty/stb)

set(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Cubism Core — static library
add_library(Live2DCubismCore STATIC IMPORTED)
set_target_properties(Live2DCubismCore PROPERTIES
  IMPORTED_LOCATION ${CORE_PATH}/lib/${ANDROID_ABI}/libLive2DCubismCore.a
  INTERFACE_INCLUDE_DIRECTORIES ${CORE_PATH}/include
)

# Cubism Framework — set LIB_NAME before including subdirectory
set(LIB_NAME Framework)
set(FRAMEWORK_SOURCE OpenGL)
add_library(${LIB_NAME} STATIC)
target_include_directories(${LIB_NAME}
  PUBLIC ${FRAMEWORK_PATH}
  PUBLIC ${CORE_PATH}/include
)
add_subdirectory(${FRAMEWORK_PATH})
target_compile_definitions(${LIB_NAME} PUBLIC CSM_TARGET_ANDROID_ES2)

# App shared library (loaded by Java via System.loadLibrary)
add_library(${APP_NAME} SHARED
  JniBridgeC.cpp
  LAppAllocator.cpp
  LAppDefine.cpp
  LAppMinimumDelegate.cpp
  LAppMinimumLive2DManager.cpp
  LAppMinimumModel.cpp
  LAppMinimumView.cpp
  LAppPal.cpp
  LAppSprite.cpp
  LAppSpriteShader.cpp
  LAppTextureManager.cpp
  TouchManager.cpp
  WallpaperEffects.cpp
)

target_include_directories(${APP_NAME} PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}
  ${FRAMEWORK_PATH}
  ${CORE_PATH}/include
  ${STB_PATH}
)

target_link_libraries(${APP_NAME}
  ${LIB_NAME}
  Live2DCubismCore
  GLESv2
  EGL
  android
  log
)
