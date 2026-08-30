package com.miru.companion;

import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.text.Layout;
import android.text.StaticLayout;
import android.text.TextPaint;

import java.nio.ByteBuffer;

/**
 * Renders a chat bubble with text into an RGBA byte array for GL texture upload.
 * All text layout uses Android Canvas (full CJK + emoji support).
 */
public class BubbleRenderer {

    public static class Result {
        public byte[] rgba;
        public int width, height;
    }

    public static Result render(String text, int screenWidth, float density) {
        if (text == null || text.isEmpty()) return null;

        // Layout params
        int maxBubbleWidth = (int) (screenWidth * 0.72f);
        float fontSize = 15.5f * density;
        int pad = (int) (14 * density);
        int tailH = (int) (10 * density);
        float radius = 14 * density;

        // Measure text
        TextPaint tp = new TextPaint(Paint.ANTI_ALIAS_FLAG);
        tp.setTextSize(fontSize);
        tp.setColor(Color.parseColor("#2D2D2D"));

        int maxTextWidth = maxBubbleWidth - pad * 2;
        StaticLayout sl = StaticLayout.Builder
                .obtain(text, 0, text.length(), tp, maxTextWidth)
                .setAlignment(Layout.Alignment.ALIGN_NORMAL)
                .setLineSpacing(2 * density, 1.0f)
                .setMaxLines(6)
                .build();

        // Compute actual text width
        int textW = 0;
        for (int i = 0; i < sl.getLineCount(); i++) {
            textW = Math.max(textW, (int) Math.ceil(sl.getLineWidth(i)));
        }

        int bw = textW + pad * 2;
        int bh = sl.getHeight() + pad * 2 + tailH;

        // Create bitmap
        Bitmap bmp = Bitmap.createBitmap(bw, bh, Bitmap.Config.ARGB_8888);
        Canvas c = new Canvas(bmp);

        // Bubble background
        Paint bg = new Paint(Paint.ANTI_ALIAS_FLAG);
        bg.setColor(Color.parseColor("#E8FFFFFF"));
        bg.setShadowLayer(3 * density, 0, 1.5f * density, Color.parseColor("#30000000"));
        RectF rect = new RectF(2 * density, 2 * density, bw - 2 * density, bh - tailH);
        c.drawRoundRect(rect, radius, radius, bg);

        // Tail triangle
        bg.clearShadowLayer();
        Path tail = new Path();
        float tx = bw / 2f;
        tail.moveTo(tx - 7 * density, bh - tailH);
        tail.lineTo(tx, bh - 1 * density);
        tail.lineTo(tx + 7 * density, bh - tailH);
        tail.close();
        c.drawPath(tail, bg);

        // Draw text
        c.save();
        c.translate(pad, pad);
        sl.draw(c);
        c.restore();

        // Convert BGRA → RGBA
        ByteBuffer buf = ByteBuffer.allocate(bw * bh * 4);
        bmp.copyPixelsToBuffer(buf);
        bmp.recycle();
        byte[] pixels = buf.array();
        for (int i = 0; i < pixels.length; i += 4) {
            byte tmp = pixels[i];       // B
            pixels[i] = pixels[i + 2];  // R → 0
            pixels[i + 2] = tmp;        // B → 2
        }

        Result r = new Result();
        r.rgba = pixels;
        r.width = bw;
        r.height = bh;
        return r;
    }
}
