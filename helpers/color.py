'''	Utilities for converting between RGB color and LAB color, which is commonly used by DICOM.
	Adapted from: https://gist.github.com/issakomi/29e48917e77201f2b73bfa5fe7b30451
	and https://github.com/dcmjs-org/dcmjs/blob/master/src/colors.js.
'''

import sys, os, glob, string, random
from typing import Optional

from client.utils.colors import hex2rgb, rgb2hex

from ..imaging.orthanc.base import RGBColor, LABColor, XYZColor



XYZ_D65_WHITE = XYZColor(0.950456, 1.000000, 1.088754)



def labf(x):
	'''	Apply LAB encoding to the provided value.
	'''
	if x >= 8.85645167903563082e-3:
		return pow(x, 0.333333333333333)

	return (841.0/108.0)*x + 4.0/29.0


def labf_inverse(x):
	'''	Apply an inverse LAB encoding to the provided value. Reverse operation
		of `lab_f`.
	'''
	if x >= 0.206896551724137931:
		y = x*x*x
	else:
		y = (108.0/841.0)*(x-(4.0/29.0))
	
	return y


def gamma_correction(x):
	'''	Apply a gamma correction to the provided value.
	'''
	if x <= 0.0031306684425005883:
		y = 12.92*x
	else:
		y = 1.055*pow(x, 0.416666666666666667)-0.055
	return y


def gamma_correction_inverse(x):
	'''	Apply an inverse gamma correction inverse to the provided value.
		Reverse operation of `gamma_correction`.
	'''
	if x <= 0.0404482362771076:
		return x/12.92

	return pow((x+0.055)/1.055, 2.4)


def dcm2cielab(val):
	'''	Parse the provided DICOM encoded color to a CIELAB representation.

		@returns sonador.imiaging.orthanc.base.LABColor
	'''
	L = (val[0]/65535.0)*100.0 					# Valid range: 0 <= L <= 100
	a = ((val[1]-32896.0)/65535.0)*255.0 		# Valid range: -128 <= a <= 127
	b = ((val[2]-32896.0)/65535.0)*255.0 		# Valid range: -128 <= b <= 127

	return LABColor(L, a, b)


def rgb2xyz(rgb:RGBColor, is_upscaled=None, norm_val=255):
	'''	Convert the provided RGB color value to an XYZ representation. Requires that the RGB
		values be normalized to 0 and 1. If the values are not scaled, the method will attempt 
		to scale them.

		@input rgb (RGBColor): RGB color to convert to XYZ.
		@input is_upscaled (bool, default=dynamically checked): flags whether the RGB values
			should be normalized.
		@input norm_val (int, default=255): value by which the RGB values should be normalized.
			8-bit depth is assumed by default.

		@returns sonador.imaging.orthanc.base.XYZColor
	'''
	# Try to determine if the color is upscaled
	if is_upscaled is None and any(v > 1 for v in rgb):
		is_upscaled = True

	# If upscaled, normalize values
	if is_upscaled:
		rgb = RGBColor(*tuple(v/norm_val for v in rgb))

	R = gamma_correction_inverse(rgb.red)
	G = gamma_correction_inverse(rgb.green)
	B = gamma_correction_inverse(rgb.blue)

	return XYZColor(
		0.4123955889674142161 * R + 0.3575834307637148171 * G + 0.1804926473817015735 * B,
		0.2125862307855955516 * R + 0.7151703037034108499 * G + 0.07220049864333622685 * B,
		0.01929721549174694484 * R + 0.1191838645808485318 * G + 0.950497125131579766 * B)


def xyz2cielab(xyz:XYZColor, white_ref:Optional[XYZColor]=XYZ_D65_WHITE):
	'''	Convert the provided XYZ color value to a CIE Lab representation.
		Encoding uses a D65 white point by default.

		@returns sonador.imaging.orthanc.base.LABColor
	'''
	X = labf(xyz.x/white_ref.x)
	Y = labf(xyz.y/white_ref.y)
	Z = labf(xyz.z/white_ref.z)

	return LABColor(116 * Y - 16, 500 * (X - Y), 200 * (Y - Z))


def rgb2cielab(rgb:RGBColor):
	'''	Convert the provided RGB lab value to a CIE Lab representation.

		@returns sonador.imaging.orthanc.base.LABColor
	'''
	return xyz2cielab(rgb2xyz(rgb))


def cielab2rgb(lcolor:LABColor, white_ref:Optional[XYZColor]=XYZ_D65_WHITE, 
		upscale=True, upscale_val=255):
	''' Convert the provided CIE Lab value to an RGB representation. 
		Encoding uses a D65 white point by default.

		@returns sonador.imaging.orthanc.base.RGBColor
	'''
	Ltmp = (lcolor.L+16.0)/116.0
	X = white_ref.x*labf_inverse(Ltmp+lcolor.a/500.0)
	Y = white_ref.y*labf_inverse(Ltmp)
	Z = white_ref.z*labf_inverse(Ltmp-lcolor.b/200.0)
	
	Rtmp =  3.2406*X-1.5372*Y-0.4986*Z
	Gtmp = -0.9689*X+1.8758*Y+0.0415*Z
	Btmp =  0.0557*X-0.2040*Y+1.0570*Z
	
	M = 0.0
	if Rtmp <= Gtmp:
		M = min(Rtmp,Btmp)
	else:
		M = min(Gtmp,Btmp)
	
	if M < 0:
		Rtmp -= M
		Gtmp -= M
		Btmp -= M
	
	R = gamma_correction(Rtmp)
	G = gamma_correction(Gtmp)
	B = gamma_correction(Btmp)

	if upscale:
		return RGBColor(*tuple(round(upscale_val*v) for v in (R,G,B)))
	
	return RGBColor(R,G,B)


def cielab2dcm(lcolor:LABColor):
	'''	Convert the provided CIE Lab encoded color to a DICOM representation.
	'''
	return (
		(lcolor.L * 65535.0) / 100.0, 			# results in 0 <= L <= 65535
		((lcolor.a + 128) * 65535.0) / 255.0, 	# results in 0 <= a <= 65535
		((lcolor.b + 128) * 65535.0) / 255.0    # results in 0 <= b <= 65535
	)


def dcm2rgb(val, *args, **kwargs):
	'''	Convert the provided DICOM encoded color to an RGB representation.

		@returns sonador.imaging.orthanc.base.RGBColor
	'''
	return cielab2rgb(dcm2cielab(val), *args, **kwargs)


def rgb2dcm(rgb:RGBColor):
	'''	Convert the provided RGB encoded color to a DICOM representation.

		@returns three-member tuple: DICOM-LAB(L,a,b)
	'''
	return cielab2dcm(rgb2cielab(rgb))


def hex2dcm(hexval):
	'''	Convert the provided hexademical encoded color to a DICOM representation.

		@returns three-member tuple: DICOM-LAB(L,a,b)
	'''
	return rgb2dcm(hex2rgb(hexval))


def dcm2hex(dcmval, *args, **kwargs):
	'''	Convert the provided DICOM encoded color to a hexademical value.

		@returns hexadecimal encoded string	
	'''
	return rgb2hex(dcm2rgb(dcmval, *args, **kwargs))
