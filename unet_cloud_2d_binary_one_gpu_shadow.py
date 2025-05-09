# -*- coding: utf-8 -*-
"""
Created on Wed Oct 19 15:27:41 2022

@author: TaniaKleynhans
"""
import os
import numpy as np
import rasterio
import time
import json
import matplotlib.pyplot as plt
from datetime import date
from patchify import patchify
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report
from tensorflow.keras.layers import Conv2D, BatchNormalization, Activation, MaxPool2D, Conv2DTranspose, Concatenate, Input
from tensorflow.keras.models import Model, load_model

from tensorflow.python.keras.losses import BinaryCrossentropy
from tensorflow.python.keras.metrics import BinaryAccuracy
from tensorflow.keras.optimizers import Adam


def load_training_data(filename, sbaf):
    """Read bands similar to VZ01.

    Args:
        filename (str): 
    """
    src = rasterio.open(filename)
    img = src.read(1)
    
    data = np.empty([img.shape[0],img.shape[1],5])

    img = src.read(2) # blue
    data[:,:,0] = sbaf["viri"]["BLUE"][0] + (img * 2.0000E-05 - 0.100000) * sbaf["viri"]["BLUE"][1]
    img = src.read(3) # green
    data[:,:,1] = sbaf["viri"]["GREEN"][0] + (img * 2.0000E-05 - 0.100000) * sbaf["viri"]["GREEN"][1]
    img = src.read(4) # red
    data[:,:,2] = sbaf["viri"]["RED"][0] + (img * 2.0000E-05 - 0.100000) * sbaf["viri"]["RED"][1]
    img = src.read(5) # NIR (similar to band 7 of VZ01 vnir)
    data[:,:,3] = sbaf["viri"]["NIR2"][0] + (img * 2.0000E-05 - 0.100000) * sbaf["viri"]["NIR2"][1]
    b10 = src.read(9)
    b11 = src.read(10)
    mask = np.copy(b10)
    b10 = b10 * 3.3420E-04 + 0.1
    b11 = b11 * 3.3420E-04 + 0.1
    # apply sabf   
    img = sbaf["liri"]["LWIR1"][0] + b10 * sbaf["liri"]["LWIR1"][1] + b10 * sbaf["liri"]["LWIR1"][2]
    data[:,:,4] = retrieve_temprature_from_LUT(img,  band ='LWIR1')
    
    mask[mask < 0] = 0
    mask[mask > 0] = 1

    for band in range(data.shape[2]):
        data[:,:,band] = data[:,:,band] * mask

    filename_lables = filename[:-8] + 'labels.tif'
    src = rasterio.open(filename_lables)
    img = src.read(1)
    label = img
    
    filename_rgb = filename[:-8] + 'photo.png'
    src = rasterio.open(filename_rgb)
    R = src.read(1)
    G = src.read(2)
    B = src.read(3)
    rgb = np.zeros((R.shape[0], R.shape[1],3))
    rgb[:,:,0] = R/np.nanmax(R)
    rgb[:,:,1] = G/np.nanmax(G)
    rgb[:,:,2] = B/np.nanmax(B)
    rgb = rgb *1.5
    
    # normalize data with values accross all scenes (subset max values skews data)

     # normalize data with values accross all scenes (subset max values skews data)
    max_training = [1.2107, 1.2107, 1.2107, 1.2107]

    for i in range(data.shape[2]-1):
        data[:,:,i] = data[:,:,i] / max_training[i] 
    # stretch temperature data (Kelvin)
    data[:,:,4] = (data[:,:,4] - 220) / (330-200)
    data[:,:,4][data[:,:,4] < 0] = 0
    
    data = 1-data
    
    return data, label, rgb    


def retrieve_temprature_from_LUT(radiance, band='LWIR1'):
    """Find apparent temperature for LIRI band from LUT.
    
    Args:
        radiance (array of floats): radiance values
        band (str): name of band to be converted
    
    Return:
        T (array of floats): Apparent temperature array same size as radiance input.
    
    """
    filepath = "spec_files/"

    if band == 'LWIR1':
        LUT = np.loadtxt(filepath + 'LUT_LWIR1.csv', delimiter=',')
    elif band == 'LWIR2':
        LUT = np.loadtxt(filepath + 'LUT_LWIR2.csv', delimiter=',')

    T = np.interp(radiance, LUT[:,0], LUT[:,1])

    return T


def load_and_format_training_data(filepath, test_scenes, classification, xy=64, steps=64):
    """Load training data and convert to correct format."""
    if classification == 'snow':
        class_num = [3]
    elif classification == 'cloud':
        class_num = [5]
    elif classification == 'shadow':
        class_num = [0, 1]

    # Load spectral band adjustment coefficients
    with open("spec_files/vz01_sbaf_landsat8.json") as sbaf_file:
        sbaf = json.load(sbaf_file)

    cnt = 0
    # plt.figure()
    for file in os.listdir(filepath):
        if '_data' in file: # and test_scenes[0] not in file and test_scenes[1] not in file:
            try:
                filename = os.path.join(filepath, file)            
                data, label, rgb = load_training_data(filename, sbaf) 
                # mask_label = np.copy(label)
                for idx in class_num:
                    label[label == idx] = 10
                label[label != 10] = 0
                label[label == 10] = 1
                   
                if cnt == 0:
                    image = data
                    mask = label
                    cnt+=1
                else:
                    image = np.hstack((image, data)) # (256,256,256) # (1000,79000,6)
                    mask = np.hstack((mask, label))
                    cnt+=1

            except:
                pass
    
    print(f"Number of scenes: {cnt}")
    
    n_classes = 2 #np.unique(mask).shape[0]
    
    # create smaller section per image 
    img_patches = patchify(image, (xy, xy, 5), step=steps)  # Step=64 for 64 patches means no overlap
    mask_patches = patchify(mask.astype(int), (xy, xy), step=steps)

    input_img = np.reshape(img_patches, (-1, img_patches.shape[3], img_patches.shape[4], img_patches.shape[5]))
    input_mask = np.reshape(mask_patches, (-1, mask_patches.shape[2], mask_patches.shape[3]))

    X_train, X_test, y_train, y_test = train_test_split(input_img, input_mask, test_size = 0.10, random_state = 2)

    return n_classes, X_train, X_test, y_train, y_test 


def conv_block(input, num_filters):
    """Define convolutional block."""
    x = Conv2D(num_filters, 3, padding="same")(input)
    x = BatchNormalization()(x)
    x = Activation("relu")(x)

    x = Conv2D(num_filters, 3, padding="same")(x)
    x = BatchNormalization()(x)
    x = Activation("relu")(x)

    return x


def encoder_block(input, num_filters):
    """Define encoder block."""
    x = conv_block(input, num_filters)
    p = MaxPool2D((2, 2))(x)
    return x, p


def decoder_block(input, skip_features, num_filters):
    """Define decoder block."""
    x = Conv2DTranspose(num_filters, (2, 2), strides=2, padding="same")(input)
    x = Concatenate()([x, skip_features])
    x = conv_block(x, num_filters)
    return x


def build_unet(input_shape, block=2):
    """Build the unet model."""
    inputs = Input(input_shape) # height, width, channels ... e.g. 128 x 128 x 6

    if block==2:
        s1, p1 = encoder_block(inputs, 64)
        s2, p2 = encoder_block(p1, 128)
        
        b1 = conv_block(p2, 256) #Bridge
        
        d1 = decoder_block(b1, s2, 128)
        d2 = decoder_block(d1, s1, 64)
        
        outputs = Conv2D(1, 1, padding="same", activation="sigmoid")(d2)  # sigmoid for binary classification
         
    elif block==3:
        s1, p1 = encoder_block(inputs, 64)
        s2, p2 = encoder_block(p1, 128)
        s3, p3 = encoder_block(p2, 256)
    
        b1 = conv_block(p3, 512)
    
        d1 = decoder_block(b1, s3, 256)
        d2 = decoder_block(d1, s2, 128)
        d3 = decoder_block(d2, s1, 64)
        
        outputs = Conv2D(1, 1, padding="same", activation="sigmoid")(d3)  # sigmoid for binary classification
        
    elif block==4:
        
        s1, p1 = encoder_block(inputs, 64)
        s2, p2 = encoder_block(p1, 128)
        s3, p3 = encoder_block(p2, 256)
        s4, p4 = encoder_block(p3, 512)
    
        b1 = conv_block(p4, 1024)
    
        d1 = decoder_block(b1, s4, 512)
        d2 = decoder_block(d1, s3, 256)
        d3 = decoder_block(d2, s2, 128)
        d4 = decoder_block(d3, s1, 64)
    
        outputs = Conv2D(1, 1, padding="same", activation="sigmoid")(d4)  # sigmoid for binary classification

    model = Model(inputs, outputs, name="U-Net")
    return model


def plotting_results(history):
    """Plot the training and validation IoU and loss at each epoch."""
    loss = history.history['loss']
    val_loss = history.history['val_loss']
    epochs = range(1, len(loss) + 1)
    plt.figure()
    plt.subplot(1,2,1)
    plt.plot(epochs, loss, 'y', label='Training loss')
    plt.plot(epochs, val_loss, 'r', label='Validation loss')
    plt.title('Training and validation loss')
    plt.xlabel('Epochs')
    plt.ylabel('Loss')
    plt.legend()
    plt.show()
    
    acc = history.history['binary_accuracy']
    val_acc = history.history['val_binary_accuracy']
    
    plt.subplot(1,2,2)
    plt.plot(epochs, acc, 'y', label='Binary accuracy')
    plt.plot(epochs, val_acc, 'r', label='Validation accuracy')
    plt.title('Training and validation Accuracy')
    plt.xlabel('Epochs')
    plt.ylabel('Accuracy')
    plt.legend()
    plt.show()


def plot_full_scene(model, test_filename, xy):
    """Predict feature from full scene input."""
    #Break the large image (volume) into patches of same size as the training images (patches)
    full_image, label, rgb = load_training_data(test_filename)
    
    boundary = 4
    steps = xy - (boundary * 2)  # to be able to remove boundary created by padding
    patches = patchify(full_image, (xy, xy, full_image.shape[2]), step=steps)  #Step=256 for 256 patches means no overlap
    # patches_new = np.reshape(patches, (-1, patches.shape[3], patches.shape[4], patches.shape[5]))

    print(full_image.shape)
    print(patches.shape)
    # print(patches_new.shape)
    
    reconstructed_image = np.zeros((full_image.shape[0], full_image.shape[1]))
    
    i_start_row = boundary

    for i in range(patches.shape[0]):
        j_start_col = boundary
        for j in range(patches.shape[1]):
            single_patch = patches[i,j,0,:,:,:]
            single_patch_input = np.expand_dims(single_patch, axis=0)
            single_patch_prediction = model.predict(single_patch_input)
            
            # remove boundaries
            single_patch_prediction_no_boundaries = single_patch_prediction[0, boundary:-boundary, boundary:-boundary, 0]
            
            # add prediction to image
            reconstructed_image[i_start_row : i_start_row + steps, j_start_col : j_start_col + steps] = single_patch_prediction_no_boundaries
            
            j_start_col = j_start_col + steps
        i_start_row = i_start_row + steps     
    
    plt.figure()
    plt.subplot(2,3,1)
    plt.imshow(rgb)
    plt.axis('off')
    plt.title('Scaled color')
    plt.show()
    plt.subplot(2,3,2)
    plt.imshow(label, cmap='Dark2', vmin=0, vmax=7)
    plt.axis('off')
    plt.title('Truth classification')
    plt.show()
    plt.subplot(2,3,3)
    plt.imshow(reconstructed_image) #, cmap='Dark2', vmin=0, vmax=7)
    plt.axis('off')
    plt.title('Predicted classification')
    plt.colorbar()
    #cbar = fig.colorbar(ax2, ticks=[0,1,2,3,4,5,6,7])
    #cbar.ax.set_yticklabels(['Cloud Shadow', 'Cloud Shadow over Water', 'Water',
    #                          'Ice/Snow','Land','Clouds','Flooded','None'])
    plt.show()
    scaled = np.where(reconstructed_image > 0.2, 1, 0)
    plt.subplot(2,3,4)
    plt.imshow(scaled)
    plt.axis('off')
    plt.title('Threshold = 0.2')
    plt.show()
    scaled = np.where(reconstructed_image > 0.5, 1, 0)
    plt.subplot(2,3,5)
    plt.imshow(scaled)
    plt.axis('off')
    plt.title('Threshold = 0.5')
    plt.show()
    scaled = np.where(reconstructed_image > 0.95, 1, 0)
    plt.subplot(2,3,6)
    plt.imshow(scaled)
    plt.axis('off')
    plt.title('Threshold = 0.95')
    plt.show()


def display_per_class_accuracy(model, X_test, y_test):
    
    Y_test = np.argmax(y_test, axis=4) # Convert one-hot to index
    y_pred = model.predict(X_test)
    test_prediction = np.argmax(y_pred, axis=4)
    
    print(classification_report(Y_test.flatten(), test_prediction.flatten()))


def main(model_filename, data_filepath, test_scenes, batch_size, epochs, classification, xy, steps, block):
    """Run application."""

    print("batch_size: ", batch_size)
    start_time = time.time()

    n_classes, X_train, X_test, y_train, y_test = load_and_format_training_data(data_filepath, test_scenes, classification, xy, steps)
    
    patches, patch_size_x, patch_size_y, channels = X_train.shape

    input_shape = [patch_size_x, patch_size_y, channels]
    print("Input shape: ", input_shape)

    model = build_unet(input_shape, block)

    # define learning rate
    LR = 0.0001
    optim = Adam(LR)
    # Complie model
    model.compile(optimizer=optim, loss=BinaryCrossentropy(), metrics=BinaryAccuracy())
    print(model.summary())
    print("Patches: ", patches)
    
    # Fit the model
    history=model.fit(X_train, 
              y_train,
              batch_size=batch_size, 
              epochs=epochs,
              verbose=1,
              validation_data=(X_test, y_test))
    
    # display_per_class_accuracy(model, X_test, y_test)
    
    end_time = time.time()
    print('Total time in min: ',(end_time - start_time)/60)

    # save model
    model.save(model_filename)

    # plotting_results(history)

    return model, history


if __name__ == "__main__":
    epochs = 100
    batch_size = [64]
    block = [2]  # number of encoder-decoder blocks
    patches = [64]  # patch size (larger needs more memory)
    classification = ['shadow']  #['snow', 'cloud', 'shadow']
    test_full_scene = 0
    test_outside_scene = 0
    run_model = 1
    test_scenes = [] # ['LC82010332014105LGN00_34', 'LC81480352013195LGN00_32']
    
    today = str(date.today())
    cnt = 0
    if run_model == 1:
        data_filepath = '/home/tkleynhans/hydrosat/data/shadow_test/'
        model_filepath = '/home/tkleynhans/hydrosat/data/models'
        for bs in batch_size:
            for bl in block:
                for cl in classification:
                    for patch in patches:
                        steps = patch - 10
                        model_fname = f'vz01_sparcs_2D_{epochs}epochs_{bs}bs_{cl}_{patch}patch_{bl}blocks_{today}.h5'
                        print(model_fname)
                        cnt += 1
                        
                        model_filename = os.path.join(model_filepath, model_fname)
                        model, history = main(model_filename, data_filepath, test_scenes, bs, epochs, cl, patch, steps, bl)
    

    #test_filename = os.path.join(data_filepath, 'LC82290562014157LGN00_24_data.tif')                  
    #plot_full_scene(model, test_filename, patch)
    

    
    
    
    
    
