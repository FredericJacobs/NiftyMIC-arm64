#!/usr/bin/python

## \file RegularizedInPlaneRegistration.py
#  \brief 
#
#  \author Michael Ebner (michael.ebner.14@ucl.ac.uk)
#  \date Nov 2016


## Import libraries
import sys
import SimpleITK as sitk
import itk
import numpy as np
from scipy.optimize import least_squares
import time
from datetime import timedelta

## Import modules
import base.PSF as psf
import base.Slice as sl
import base.Stack as st
import utilities.SimpleITKHelper as sitkh
import utilities.PythonHelper as ph
from registration.RegistrationBase import RegistrationBase

## Pixel type of used 3D ITK image
PIXEL_TYPE = itk.D

## ITK image type
IMAGE_TYPE = itk.Image[PIXEL_TYPE, 3]


class RegularizedInPlaneRegistration(RegistrationBase):

    def __init__(self, fixed=None, moving=None, use_fixed_mask=False, use_moving_mask=False, use_verbose=False):

        ## Run constructor of superclass
        RegistrationBase.__init__(self, fixed=fixed, moving=moving, use_fixed_mask=use_fixed_mask, use_moving_mask=use_moving_mask, use_verbose=use_verbose)

        self._nda_shape = np.array(self._fixed.sitk.GetSize())[::-1]
        
        self._N_slices = self._nda_shape[0]
        self._N_2D_voxels = self._nda_shape[1:].prod()

        self._N_residual = (self._N_slices-1) * self._N_2D_voxels

        self._registration_transforms_sitk = [None]*self._N_slices
        self._parameters = [None]*self._N_slices


    def get_parameters(self):
        return np.array(self._parameters)


    def get_registration_transform_sitk(self):
        return np.array(self._registration_transforms_sitk)


    def get_registered_stack(self):
   
        return st.Stack.from_stack(self._stack_corrected)



    def run_regularized_rigid_inplane_registration(self):
        
        transforms_PP_3D_sitk = self._get_list_of_3D_rigid_transforms_of_slices(self._fixed)

        ## Get projected (and masked) slices
        self._2D_projected_slices = self._get_2D_projected_and_masked_slices(self._fixed, transforms_PP_3D_sitk)


        self._degrees_of_freedom = 3
        transforms_sitk = [sitk.Euler2DTransform()] * self._N_slices

        self._transform_PI_fixed_2D_sitk = sitkh.get_sitk_affine_transform_from_sitk_image(self._2D_projected_slices[0].sitk)
        self._fixed_grid_2D_sitk = sitk.Image(self._2D_projected_slices[0].sitk)

        parameters0 = np.zeros((self._N_slices-1, self._degrees_of_freedom)).flatten()
        
        fun = lambda x: self._get_residual_data_fit(x, transforms_sitk)

        # Non-linear least-squares method:
        time_start = ph.start_timing()
        # res = least_squares(fun=fun, x0=parameters0, method='trf', loss='soft_l1', verbose=2) 
        # res = least_squares(fun=fun, x0=parameters0, method='lm', loss='linear', verbose=1) 
        res = least_squares(fun=fun, x0=parameters0, method='dogbox', loss='linear', verbose=2) 
        self._elapsed_time = ph.stop_timing(time_start)

        ## Get parameters and add additional row for first slice
        parameters = res.x.reshape(-1, self._degrees_of_freedom)
        self._parameters = np.concatenate((np.zeros((1,self._degrees_of_freedom)), parameters), axis=0)

        ## Get corrected stack based on registration
        self._stack_corrected, self._registration_transforms_sitk = self._apply_motion_correction(transforms_PP_3D_sitk)


    def _apply_motion_correction(self, transforms_PP_3D_sitk):

        stack_corrected = st.Stack.from_stack(self._fixed, self._fixed.get_filename()+"_registered")
        slices = stack_corrected.get_slices()
        transform_2D_sitk = sitk.Euler2DTransform()

        registration_transforms_sitk = [None] * self._N_slices

        for i in range(0, self._N_slices):
            transform_2D_sitk.SetParameters(self._parameters[i,:])
            transform_2D_sitk = sitk.Euler2DTransform(transform_2D_sitk.GetInverse())

            ## Expand to 3D transform
            transform_3D_sitk = self._get_3D_from_2D_rigid_transform_sitk(transform_2D_sitk)

            ## Compose to 3D in-plane transform
            affine_transform_sitk = sitkh.get_composite_sitk_affine_transform(transform_3D_sitk, transforms_PP_3D_sitk[i])
            affine_transform_sitk = sitkh.get_composite_sitk_affine_transform(sitk.AffineTransform(transforms_PP_3D_sitk[i].GetInverse()), affine_transform_sitk)

            slices[i].update_motion_correction(affine_transform_sitk)
            
            registration_transforms_sitk[i] = affine_transform_sitk

        return stack_corrected, registration_transforms_sitk



    def _get_residual_data_fit(self, parameters_vec, transforms_sitk, interpolator=sitk.sitkLinear):

        residual = np.zeros((self._N_slices-1, self._N_2D_voxels))
        parameters = parameters_vec.reshape(-1, self._degrees_of_freedom)            

        ## Get transformed image of first index
        # transforms_sitk[0].SetParameters(parameters[0,:])
        # warped_grid_2D_i = sitkh.get_transformed_sitk_image(self._fixed_grid_2D_sitk, transforms_sitk[0])
        # slice_i_sitk = sitk.Resample(self._2D_projected_slices[0].sitk, self._fixed_grid_2D_sitk, transforms_sitk[0], interpolator)
        slice_i_nda = sitk.GetArrayFromImage(self._2D_projected_slices[0].sitk)

        for i in range(0, self._N_slices-1):
            transforms_sitk[i+1].SetParameters(parameters[i,:])
            slice_ip1_sitk = sitk.Resample(self._2D_projected_slices[i+1].sitk, self._fixed_grid_2D_sitk, transforms_sitk[i+1], interpolator)
            slice_ip1_nda = sitk.GetArrayFromImage(slice_ip1_sitk)

            residual[i,:] = (slice_i_nda - slice_ip1_nda).flatten()

            slice_i_nda = slice_ip1_nda

        return residual.flatten()
        

    ###-------------------------------------------------------------------------
    # \brief      Get 2D slice for in-plane operations as projection from 3D
    #             space onto the x-y-plane.
    # \date       2016-09-20 22:42:45+0100
    #
    # Get 2D slice for in-plane operations, i.e. where in-plane motion can be
    # captured by only using 2D transformations. Depending on previous
    # operations the slice can already be shifted due to previous in-plane
    # registration steps. The 3D affine transformation transform_PP_sitk
    # indicates the original position of the corresponding original 3D slice.
    # Given that it operates from the physical 3D space to the physical 3D
    # space the name 'PP' is given.
    #
    # \param      self               The object
    # \param      slice_3D           The slice 3d
    # \param      transform_PP_sitk  sitk
    #
    # \return     Projected 2D slice onto x-y-plane of 3D stack as Slice object
    #
    # TODO: Change to make simpler
    def _get_2D_projected_and_masked_slices(self, stack, transforms_PP_3D_sitk):

        slices_3D = stack.get_slices()
        slices_2D = [None]*self._N_slices

        for i in range(0, self._N_slices):

            ## Create copy of the slices (since its header will be updated)
            slice_3D = sl.Slice.from_slice(slices_3D[i])

            ## Get current transform from image to physical space of slice
            T_PI = sitkh.get_sitk_affine_transform_from_sitk_image(slice_3D.sitk)

            ## Get transform to align slice with physical coordinate system (perhaps already shifted there) 
            T_PI_align = sitkh.get_composite_sitk_affine_transform(transforms_PP_3D_sitk[i], T_PI)

            ## Set direction and origin of image accordingly
            direction = sitkh.get_sitk_image_direction_from_sitk_affine_transform(T_PI_align, slice_3D.sitk)
            origin = sitkh.get_sitk_image_origin_from_sitk_affine_transform(T_PI_align, slice_3D.sitk)

            slice_3D.sitk.SetDirection(direction)
            slice_3D.sitk.SetOrigin(origin)
            slice_3D.sitk_mask.SetDirection(direction)
            slice_3D.sitk_mask.SetOrigin(origin)

            ## Get filename and slice number for name propagation
            filename = slice_3D.get_filename()
            slice_number = slice_3D.get_slice_number()

            slice_2D_sitk = slice_3D.sitk[:,:,0]

            if self._use_fixed_mask:
                caster = sitk.CastImageFilter()
                caster.SetOutputPixelType( slice_2D_sitk.GetPixelIDValue() )
                slice_2D_sitk = slice_2D_sitk * caster.Execute(slice_3D.sitk_mask[:,:,0])

            slices_2D[i] = sl.Slice.from_sitk_image(slice_2D_sitk, dir_input=None, filename=filename, slice_number=slice_number)

        return slices_2D



    ##-------------------------------------------------------------------------
    # \brief      Get the 3D rigid transforms to arrive at the positions of
    #             original 3D slices starting from the physically aligned
    #             space with the main image axes.
    # \date       2016-09-20 23:37:05+0100
    #
    # The rigid transform is given as composed translation and rotation
    # transform, i.e. T_PP = (T_t \circ T_rot)^{-1}.
    #
    # \param      self  The object
    #
    # \return     List of 3D rigid transforms (sitk.AffineTransform(3) objects)
    #             to arrive at the positions of the original 3D slices.
    #
    # TODO: Change to make simpler
    def _get_list_of_3D_rigid_transforms_of_slices(self, stack):

        N_slices = stack.get_number_of_slices()
        slices = stack.get_slices()

        transforms_PP_3D_sitk = [None]*N_slices

        for i in range(0, N_slices):
            slice_3D_sitk = slices[i].sitk

            ## Extract origin and direction matrix from slice:
            origin_3D_sitk = np.array(slice_3D_sitk.GetOrigin())
            direction_3D_sitk = np.array(slice_3D_sitk.GetDirection())

            ## Define rigid transformation
            transform_PP_sitk = sitk.AffineTransform(3)
            transform_PP_sitk.SetMatrix(direction_3D_sitk)
            transform_PP_sitk.SetTranslation(origin_3D_sitk)

            transforms_PP_3D_sitk[i] = sitk.AffineTransform(transform_PP_sitk.GetInverse())

        return transforms_PP_3D_sitk
        # for i in range(0, self._fixed.get_number_of_slices()):

    
    ##-------------------------------------------------------------------------
    # \brief      Create 3D from 2D transform.
    # \date       2016-09-20 23:18:55+0100
    #
    # The generated 3D transform performs in-plane operations in case the
    # physical coordinate system is aligned with the axis of the stack/slice
    #
    # \param      self                     The object
    # \param      rigid_transform_2D_sitk  sitk.Euler2DTransform object
    #
    # \return     sitk.Euler3DTransform object.
    #
    def _get_3D_from_2D_rigid_transform_sitk(self, rigid_transform_2D_sitk):
    
        # Get parameters of 2D registration
        angle_z, translation_x, translation_y = rigid_transform_2D_sitk.GetParameters()
        center_x, center_y = rigid_transform_2D_sitk.GetCenter()

        # Expand obtained translation to 3D vector
        translation_3D = (translation_x, translation_y, 0)
        center_3D = (center_x, center_y, 0)

        # Create 3D rigid transform based on 2D
        rigid_transform_3D = sitk.Euler3DTransform()
        rigid_transform_3D.SetRotation(0,0,angle_z)
        rigid_transform_3D.SetTranslation(translation_3D)
        rigid_transform_3D.SetFixedParameters(center_3D)
        
        return rigid_transform_3D


