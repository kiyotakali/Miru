/**
 * JNI bridge for Miru Live2D wallpaper.
 * Based on Live2D Cubism SDK Minimum sample, adapted for WallpaperService.
 */

#include <jni.h>
#include "JniBridgeC.hpp"
#include "LAppMinimumDelegate.hpp"
#include "LAppPal.hpp"

using namespace Csm;

static JavaVM* g_JVM;
static jclass  g_JniBridgeJavaClass;
static jmethodID g_LoadFileMethodId;

JNIEnv* GetEnv()
{
    JNIEnv* env = NULL;
    g_JVM->GetEnv(reinterpret_cast<void **>(&env), JNI_VERSION_1_6);
    return env;
}

jint JNICALL JNI_OnLoad(JavaVM* vm, void* reserved)
{
    g_JVM = vm;

    JNIEnv *env;
    if (vm->GetEnv(reinterpret_cast<void **>(&env), JNI_VERSION_1_6) != JNI_OK)
    {
        return JNI_ERR;
    }

    jclass clazz = env->FindClass("com/miru/companion/Live2DBridge");
    g_JniBridgeJavaClass = reinterpret_cast<jclass>(env->NewGlobalRef(clazz));
    g_LoadFileMethodId = env->GetStaticMethodID(g_JniBridgeJavaClass, "LoadFile", "(Ljava/lang/String;)[B");

    return JNI_VERSION_1_6;
}

void JNICALL JNI_OnUnload(JavaVM *vm, void *reserved)
{
    JNIEnv *env = GetEnv();
    env->DeleteGlobalRef(g_JniBridgeJavaClass);
}

char* JniBridgeC::LoadFileAsBytesFromJava(const char* filePath, unsigned int* outSize)
{
    JNIEnv *env = GetEnv();
    jbyteArray obj = (jbyteArray)env->CallStaticObjectMethod(g_JniBridgeJavaClass, g_LoadFileMethodId, env->NewStringUTF(filePath));

    if (!obj)
    {
        return NULL;
    }

    *outSize = static_cast<unsigned int>(env->GetArrayLength(obj));
    char* buffer = new char[*outSize];
    env->GetByteArrayRegion(obj, 0, *outSize, reinterpret_cast<jbyte *>(buffer));

    return buffer;
}

void JniBridgeC::MoveTaskToBack()
{
    // No-op for wallpaper service
}

extern "C"
{
    JNIEXPORT void JNICALL
    Java_com_miru_companion_Live2DBridge_nativeOnStart(JNIEnv *env, jclass type)
    {
        LAppMinimumDelegate::GetInstance()->OnStart();
    }

    JNIEXPORT void JNICALL
    Java_com_miru_companion_Live2DBridge_nativeOnPause(JNIEnv *env, jclass type)
    {
        LAppMinimumDelegate::GetInstance()->OnPause();
    }

    JNIEXPORT void JNICALL
    Java_com_miru_companion_Live2DBridge_nativeOnStop(JNIEnv *env, jclass type)
    {
        LAppMinimumDelegate::GetInstance()->OnStop();
    }

    JNIEXPORT void JNICALL
    Java_com_miru_companion_Live2DBridge_nativeOnDestroy(JNIEnv *env, jclass type)
    {
        LAppMinimumDelegate::GetInstance()->OnDestroy();
    }

    JNIEXPORT void JNICALL
    Java_com_miru_companion_Live2DBridge_nativeOnSurfaceCreated(JNIEnv *env, jclass type)
    {
        LAppMinimumDelegate::GetInstance()->OnSurfaceCreate();
    }

    JNIEXPORT void JNICALL
    Java_com_miru_companion_Live2DBridge_nativeOnSurfaceChanged(JNIEnv *env, jclass type, jint width, jint height)
    {
        LAppMinimumDelegate::GetInstance()->OnSurfaceChanged(width, height);
    }

    JNIEXPORT void JNICALL
    Java_com_miru_companion_Live2DBridge_nativeOnDrawFrame(JNIEnv *env, jclass type)
    {
        LAppMinimumDelegate::GetInstance()->Run();
    }

    JNIEXPORT void JNICALL
    Java_com_miru_companion_Live2DBridge_nativeOnTouchesBegan(JNIEnv *env, jclass type, jfloat pointX, jfloat pointY)
    {
        LAppMinimumDelegate::GetInstance()->OnTouchBegan(pointX, pointY);
    }

    JNIEXPORT void JNICALL
    Java_com_miru_companion_Live2DBridge_nativeOnTouchesEnded(JNIEnv *env, jclass type, jfloat pointX, jfloat pointY)
    {
        LAppMinimumDelegate::GetInstance()->OnTouchEnded(pointX, pointY);
    }

    JNIEXPORT void JNICALL
    Java_com_miru_companion_Live2DBridge_nativeOnTouchesMoved(JNIEnv *env, jclass type, jfloat pointX, jfloat pointY)
    {
        LAppMinimumDelegate::GetInstance()->OnTouchMoved(pointX, pointY);
    }

    JNIEXPORT void JNICALL
    Java_com_miru_companion_Live2DBridge_nativeSetBubbleTexture(JNIEnv *env, jclass type, jbyteArray rgba, jint width, jint height)
    {
        jbyte* data = env->GetByteArrayElements(rgba, nullptr);
        LAppMinimumDelegate::GetInstance()->SetBubbleTexture(
            reinterpret_cast<const unsigned char*>(data), width, height);
        env->ReleaseByteArrayElements(rgba, data, JNI_ABORT);
    }

    JNIEXPORT void JNICALL
    Java_com_miru_companion_Live2DBridge_nativeClearBubble(JNIEnv *env, jclass type)
    {
        LAppMinimumDelegate::GetInstance()->ClearBubble();
    }

    JNIEXPORT void JNICALL
    Java_com_miru_companion_Live2DBridge_nativeSetEmotionState(JNIEnv *env, jclass type, jfloat valence, jfloat arousal)
    {
        LAppMinimumDelegate::GetInstance()->SetEmotionState(valence, arousal);
    }
}
